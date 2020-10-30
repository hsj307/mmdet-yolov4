# Copyright (c) 2019 Western Digital Corporation or its affiliates.

from ..builder import DETECTORS
from .single_stage import SingleStageDetector
from mmcv.runner import Hook, Fp16OptimizerHook, HOOKS, OptimizerHook
from mmcv.parallel import is_module_wrapper
import math
from torch.cuda.amp import GradScaler, autocast
from ...datasets import PIPELINES
from ...datasets.pipelines.compose import Compose
import mmcv
import numpy as np
import os.path as osp
import random


@DETECTORS.register_module()
class YOLOV4(SingleStageDetector):

    def __init__(self,
                 backbone,
                 neck,
                 bbox_head,
                 train_cfg=None,
                 test_cfg=None,
                 pretrained=None,
                 use_amp=True):
        super(YOLOV4, self).__init__(backbone, neck, bbox_head, train_cfg,
                                     test_cfg, pretrained)
        self.use_amp = use_amp

    def forward_train(self, *wargs, **kwargs):
        if self.use_amp:
            with autocast():
                return super(YOLOV4, self).forward_train(*wargs, **kwargs)
        else:
            return super(YOLOV4, self).forward_train(*wargs, **kwargs)

    def simple_test(self, *wargs, **kwargs):
        if self.use_amp:
            with autocast():
                return super(YOLOV4, self).simple_test(*wargs, **kwargs)
        else:
            return super(YOLOV4, self).simple_test(*wargs, **kwargs)


@HOOKS.register_module()
class AMPGradAccumulateOptimizerHook(OptimizerHook):
    def __init__(self, *wargs, **kwargs):
        self.accumulation = kwargs.pop('accumulation', 1)
        self.scaler = GradScaler()
        super(AMPGradAccumulateOptimizerHook, self).__init__(*wargs, **kwargs)
        if self.grad_clip is not None:
            self.grad_clip_base = self.grad_clip['max_norm']

    def before_run(self, runner):
        assert hasattr(runner.model.module,
                       'use_amp') and runner.model.module.use_amp, 'model should support AMP when using this optimizer hook!'

    def before_train_iter(self, runner):
        if runner.iter % self.accumulation == 0:
            runner.model.zero_grad()
            runner.optimizer.zero_grad()

    def after_train_iter(self, runner):
        scaled_loss = self.scaler.scale(runner.outputs['loss'])
        scaled_loss.backward()

        if (runner.iter + 1) % self.accumulation == 0:
            if self.grad_clip is not None:
                scale = self.scaler.get_scale()
                self.grad_clip['max_norm'] = self.grad_clip_base * scale
                grad_norm = self.clip_grads(runner.model.parameters())
                if grad_norm is not None:
                    # Add grad norm to the logger
                    runner.log_buffer.update({'grad_norm': float(grad_norm) / float(scale),
                                              'grad_scale': float(scale)},
                                             runner.outputs['num_samples'])
            self.scaler.step(runner.optimizer)
            self.scaler.update()


@HOOKS.register_module()
class Fp16GradAccumulateOptimizerHook(Fp16OptimizerHook):
    def __init__(self, *wargs, **kwargs):
        self.accumulation = kwargs.pop('accumulation', 1)
        super(Fp16GradAccumulateOptimizerHook, self).__init__(*wargs, **kwargs)

    def before_run(self, runner):
        super(Fp16GradAccumulateOptimizerHook, self).before_run(runner)
        runner.model.zero_grad()
        runner.optimizer.zero_grad()

    def before_train_iter(self, runner):
        if runner.iter % self.accumulation == 0:
            runner.model.zero_grad()
            runner.optimizer.zero_grad()

    def after_train_iter(self, runner):
        """Backward optimization steps for Mixed Precision Training.

        1. Scale the loss by a scale factor.
        2. Backward the loss to obtain the gradients (fp16).
        3. Copy gradients from the model to the fp32 weight copy.
        4. Scale the gradients back and update the fp32 weight copy.
        5. Copy back the params from fp32 weight copy to the fp16 model.
        """
        # clear grads of last iteration
        if (runner.iter + 1) % self.accumulation == 0:
            model_zero_grad = runner.model.zero_grad
            optimizer_zero_grad = runner.optimizer.zero_grad

            def dummyfun(*args):
                pass

            runner.model.zero_grad = dummyfun
            runner.optimizer.zero_grad = dummyfun

            super(Fp16GradAccumulateOptimizerHook, self).after_train_iter(runner)

            runner.model.zero_grad = model_zero_grad
            runner.optimizer.zero_grad = optimizer_zero_grad
        else:
            scaled_loss = runner.outputs['loss'] * self.loss_scale
            scaled_loss.backward()


@HOOKS.register_module()
class LrBiasPreHeatHook(Hook):
    def __init__(self,
                 preheat_iters=2000,
                 preheat_ratio=10.):

        self.preheat_iters = preheat_iters
        self.preheat_ratio = preheat_ratio

        self.bias_base_lr = {}  # initial lr for all param groups

    def before_run(self, runner):
        # NOTE: when resuming from a checkpoint, if 'initial_lr' is not saved,
        # it will be set according to the optimizer params
        if len(runner.optimizer.param_groups) != len([*runner.model.parameters()]):
            runner.logger.warning(f"optimizer config does not support preheat because"
                                  " it is not using seperate param-group for each parameter")
            return
        for group_ind, (name, param) in enumerate(runner.model.named_parameters()):
            if '.bias' in name:
                group = runner.optimizer.param_groups[group_ind]
                self.bias_base_lr[group_ind] = group['lr']

    def before_train_iter(self, runner):
        if runner.iter < self.preheat_iters:
            prog = runner.iter / self.preheat_iters
            cur_ratio = (self.preheat_ratio - 1) * (1 - prog) + 1
            for group_ind, lr_init_value in self.bias_base_lr.items():
                runner.optimizer.param_groups[group_ind]['lr'] = cur_ratio * lr_init_value


@HOOKS.register_module()
class YOLOV4EMAHook(Hook):
    r"""Exponential Moving Average Hook.

    Use Exponential Moving Average on all parameters of model in training
    process. All parameters have a ema backup, which update by the formula
    as below. EMAHook takes priority over EvalHook and CheckpointSaverHook.

        .. math::

            \text{Xema_{t+1}} = (1 - \text{momentum}) \times
            \text{Xema_{t}} +  \text{momentum} \times X_t

    Args:
        momentum (float): The momentum used for updating ema parameter.
            Defaults to 0.0002.
        interval (int): Update ema parameter every interval iteration.
            Defaults to 1.
        warm_up (int): During first warm_up steps, we may use smaller momentum
            to update ema parameters more slowly. Defaults to 100.
        resume_from (str): The checkpoint path. Defaults to None.
    """

    def __init__(self,
                 momentum=0.9999,
                 interval=2,
                 warm_up=2000,
                 resume_from=None):
        assert isinstance(interval, int) and interval > 0
        self.warm_up = warm_up
        self.interval = interval
        assert momentum > 0 and momentum < 1
        self.momentum = momentum
        self.checkpoint = resume_from

    def before_run(self, runner):
        """To resume model with it's ema parameters more friendly.

        Register ema parameter as ``named_buffer`` to model
        """
        model = runner.model
        if is_module_wrapper(model):
            model = model.module
        self.param_ema_buffer = {}
        self.model_parameters = dict(model.named_parameters(recurse=True))
        for name, value in self.model_parameters.items():
            # "." is not allowed in module's buffer name
            buffer_name = f"ema_{name.replace('.', '_')}"
            self.param_ema_buffer[name] = buffer_name
            model.register_buffer(buffer_name, value.data.clone())
        self.model_buffers = dict(model.named_buffers(recurse=True))
        if self.checkpoint is not None:
            runner.resume(self.checkpoint)

    def after_train_iter(self, runner):
        """Update ema parameter every self.interval iterations."""
        if (runner.iter + 1) % self.interval != 0:
            return
        for name, parameter in self.model_parameters.items():
            momentum = self.momentum * (1 - math.exp(-runner.iter / self.warm_up))
            if parameter.dtype.is_floating_point:
                buffer_name = self.param_ema_buffer[name]
                buffer_parameter = self.model_buffers[buffer_name]
                buffer_parameter.mul_(momentum).add_(1 - momentum, parameter.data)

    def after_train_epoch(self, runner):
        """We load parameter values from ema backup to model before the
        EvalHook."""
        self._swap_ema_parameters()

    def before_train_epoch(self, runner):
        """We recover model's parameter from ema backup after last epoch's
        EvalHook."""
        self._swap_ema_parameters()

    def _swap_ema_parameters(self):
        """Swap the parameter of model with parameter in ema_buffer."""
        for name, value in self.model_parameters.items():
            temp = value.data.clone()
            ema_buffer = self.model_buffers[self.param_ema_buffer[name]]
            value.data.copy_(ema_buffer.data)
            ema_buffer.data.copy_(temp)


@PIPELINES.register_module()
class RoundPad(object):
    """Pad the image & mask.

    There are two padding modes: (1) pad to a fixed size and (2) pad to the
    minimum size that is divisible by some number.
    Added keys are "pad_shape", "pad_fixed_size", "pad_size_divisor",

    Args:
        size (tuple, optional): Fixed padding size.
        size_divisor (int, optional): The divisor of padded size.
        pad_val (float, optional): Padding value, 0 by default.
    """

    def __init__(self, size=None, size_divisor=None, pad_val=0):
        self.size = size
        self.size_divisor = size_divisor
        self.pad_val = pad_val
        # only one of size and size_divisor should be valid
        assert size is not None or size_divisor is not None
        assert size is None or size_divisor is None

    def __call__(self, results):
        """Call function to pad images, masks, semantic segmentation maps.

        Args:
            results (dict): Result dict from loading pipeline.

        Returns:
            dict: Updated result dict.
        """
        """Pad images according to ``self.size``."""
        for key in results.get('img_fields', ['img']):
            img = results[key]
            ori_h, ori_w = img.shape[:2]
            if self.size is not None:
                pad_h, pad_w = self.size
            elif self.size_divisor is not None:
                divisor = self.size_divisor
                pad_h = int(np.ceil(ori_h / divisor)) * divisor
                pad_w = int(np.ceil(ori_w / divisor)) * divisor

            pad_top = (pad_h - ori_h) // 2
            pad_bottom = pad_h - ori_h - pad_top
            pad_left = (pad_w - ori_w) // 2
            par_right = pad_w - ori_w - pad_left
            padded_img = mmcv.impad(
                results[key], padding=(pad_left, pad_top, par_right, pad_bottom), pad_val=self.pad_val)
            results[key] = padded_img

        results['pad_shape'] = padded_img.shape
        results['pad_fixed_size'] = self.size
        results['pad_size_divisor'] = self.size_divisor

        # crop bboxes accordingly and clip to the image boundary
        for key in results.get('bbox_fields', []):
            # e.g. gt_bboxes and gt_bboxes_ignore
            bbox_offset = np.array([pad_left, pad_top, pad_left, pad_top],
                                   dtype=np.float32)
            results[key] += bbox_offset
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f'(size={self.size}, '
        repr_str += f'size_divisor={self.size_divisor}, '
        repr_str += f'pad_val={self.pad_val})'
        return repr_str


@PIPELINES.register_module()
class MosaicPipeline(object):
    """Load an image from file.

    Required keys are "img_prefix" and "img_info" (a dict that must contain the
    key "filename"). Added or updated keys are "filename", "img", "img_shape",
    "ori_shape" (same as `img_shape`), "pad_shape" (same as `img_shape`),
    "scale_factor" (1.0) and "img_norm_cfg" (means=0 and stds=1).

    Args:
        to_float32 (bool): Whether to convert the loaded image to a float32
            numpy array. If set to False, the loaded image is an uint8 array.
            Defaults to False.
        color_type (str): The flag argument for :func:`mmcv.imfrombytes`.
            Defaults to 'color'.
        file_client_args (dict): Arguments to instantiate a FileClient.
            See :class:`mmcv.fileio.FileClient` for details.
            Defaults to ``dict(backend='disk')``.
    """

    def __init__(self,
                 individual_pipeline,
                 pad_val=0):
        self.individual_pipeline = Compose(individual_pipeline)
        self.pad_val = pad_val

    def __call__(self, results):
        input_results = results.copy()
        mosaic_results = [results]
        dataset = results['dataset']
        # load another 3 images
        for _ in range(3):
            idx = random.randint(0, len(dataset) - 1)
            img_info = dataset.data_infos[idx]
            ann_info = dataset.get_ann_info(idx)
            _results = dict(img_info=img_info, ann_info=ann_info)
            if dataset.proposals is not None:
                _results['proposals'] = dataset.proposals[idx]
            dataset.pre_pipeline(_results)
            mosaic_results.append(_results)

        for idx in range(4):
            mosaic_results[idx] = self.individual_pipeline(mosaic_results[idx])

        shapes = [results['pad_shape'] for results in mosaic_results]
        yc = max(shapes[0][0], shapes[1][0])  # decided by the height of top 2 images
        xc = max(shapes[0][1], shapes[2][1])  # decided by the width of left 2 images
        canvas_height = yc + max(shapes[2][0], shapes[3][0])  # decided by yc and the height of bottom 2 images
        canvas_width = xc + max(shapes[1][1], shapes[3][1])  # decided by xc and the width of right 2 images
        canvas_shape = (canvas_height, canvas_width, shapes[0][2])

        # base image with 4 tiles
        canvas = dict()
        for key in mosaic_results[0].get('img_fields', []):
            canvas[key] = np.full(canvas_shape, self.pad_val, dtype=np.uint8)
        for i, results in enumerate(mosaic_results):
            h, w = results['pad_shape'][:2]
            # place img in img4
            if i == 0:  # top left
                x1, y1, x2, y2 = xc - w, yc - h, xc, yc  # xmin, ymin, xmax, ymax (large image)
            elif i == 1:  # top right
                x1, y1, x2, y2 = xc, yc - h, xc + w, yc
            elif i == 2:  # bottom left
                x1, y1, x2, y2 = xc - w, yc, xc, yc + h
            elif i == 3:  # bottom right
                x1, y1, x2, y2 = xc, yc, xc + w, yc + h

            for key in mosaic_results[0].get('img_fields', []):
                canvas[key][y1:y2, x1:x2] = results[key]

            for key in results.get('bbox_fields', []):
                bboxes = results[key]
                bboxes[:, 0::2] = bboxes[:, 0::2] + x1
                bboxes[:, 1::2] = bboxes[:, 1::2] + y1
                results[key] = bboxes

        output_results = input_results
        output_results['img_fields'] = mosaic_results[0].get('img_fields', [])
        output_results['bbox_fields'] = mosaic_results[0].get('bbox_fields', [])
        for key in output_results['img_fields']:
            output_results[key] = canvas[key]

        for key in output_results['bbox_fields']:
            output_results[key] = np.concatenate([r[key] for r in mosaic_results], axis=0)

        output_results['gt_labels'] = np.concatenate([r['gt_labels'] for r in mosaic_results], axis=0)

        output_results['img_shape'] = canvas_shape
        output_results['ori_shape'] = canvas_shape

        return output_results

    def __repr__(self):
        repr_str = (f'{self.__class__.__name__}('
                    f'individual_pipeline={self.individual_pipeline}, '
                    f'pad_val={self.pad_val})')
        return repr_str
