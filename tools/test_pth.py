# ---------------------------------------------
# Copyright (c) OpenMMLab. All rights reserved.
# ---------------------------------------------
#  Modified by Zhiqi Li
# ---------------------------------------------
import argparse
import mmcv
import os
import torch
import warnings
from mmcv import Config, DictAction
from mmcv.cnn import fuse_conv_bn
from mmcv.parallel import MMDataParallel, MMDistributedDataParallel
from mmcv.runner import (get_dist_info, init_dist, load_checkpoint,
                         wrap_fp16_model)

from mmdet3d.apis import single_gpu_test
from mmdet3d.datasets import build_dataset
from projects.mmdet3d_plugin.datasets.builder import build_dataloader
from mmdet3d.models import build_model
from mmdet.apis import set_random_seed
from projects.mmdet3d_plugin.core.apis.test import custom_multi_gpu_test
from mmdet.datasets import replace_ImageToTensor
import time
import os.path as osp

from tools.onnx_utils import nuscenceData, PetrWrapper, get_onnx_model

def parse_args():
    parser = argparse.ArgumentParser(
        description='MMDet test (and eval) a model')
    parser.add_argument('config',help='test config file path')
    parser.add_argument('checkpoint', help='checkpoint file')
    parser.add_argument('--out', help='output result file in pickle format')
    parser.add_argument(
        '--fuse-conv-bn',
        action='store_true',
        help='Whether to fuse conv and bn, this will slightly increase'
        'the inference speed')
    parser.add_argument(
        '--format-only',
        action='store_true',
        help='Format the output results without perform evaluation. It is'
        'useful when you want to format the result to a specific format and '
        'submit it to the test server')
    parser.add_argument(
        '--eval',
        type=str,
        nargs='+',
        help='evaluation metrics, which depends on the dataset, e.g., "bbox",'
        ' "segm", "proposal" for COCO, and "mAP", "recall" for PASCAL VOC')
    parser.add_argument('--show', action='store_true', help='show results')
    parser.add_argument(
        '--show-dir', help='directory where results will be saved')
    parser.add_argument(
        '--gpu-collect',
        action='store_true',
        help='whether to use gpu to collect results.')
    parser.add_argument(
        '--tmpdir',
        help='tmp directory used for collecting results from multiple '
        'workers, available when gpu-collect is not specified')
    parser.add_argument('--seed', type=int, default=0, help='random seed')
    parser.add_argument(
        '--deterministic',
        action='store_true',
        help='whether to set deterministic options for CUDNN backend.')
    parser.add_argument(
        '--cfg-options',
        nargs='+',
        action=DictAction,
        help='override some settings in the used config, the key-value pair '
        'in xxx=yyy format will be merged into config file. If the value to '
        'be overwritten is a list, it should be like key="[a,b]" or key=a,b '
        'It also allows nested list/tuple values, e.g. key="[(a,b),(c,d)]" '
        'Note that the quotation marks are necessary and that no white space '
        'is allowed.')
    parser.add_argument(
        '--options',
        nargs='+',
        action=DictAction,
        help='custom options for evaluation, the key-value pair in xxx=yyy '
        'format will be kwargs for dataset.evaluate() function (deprecate), '
        'change to --eval-options instead.')
    parser.add_argument(
        '--eval-options',
        nargs='+',
        action=DictAction,
        help='custom options for evaluation, the key-value pair in xxx=yyy '
        'format will be kwargs for dataset.evaluate() function')
    parser.add_argument(
        '--launcher',
        choices=['none', 'pytorch', 'slurm', 'mpi'],
        default='none',
        help='job launcher')
    parser.add_argument('--local_rank', type=int, default=0)
    args = parser.parse_args()
    if 'LOCAL_RANK' not in os.environ:
        os.environ['LOCAL_RANK'] = str(args.local_rank)

    if args.options and args.eval_options:
        raise ValueError(
            '--options and --eval-options cannot be both specified, '
            '--options is deprecated in favor of --eval-options')
    if args.options:
        warnings.warn('--options is deprecated in favor of --eval-options')
        args.eval_options = args.options
    return args


def main():
    args = parse_args()

    assert args.out or args.eval or args.format_only or args.show \
        or args.show_dir, \
        ('Please specify at least one operation (save/eval/format/show the '
         'results / save the results) with the argument "--out", "--eval"'
         ', "--format-only", "--show" or "--show-dir"')

    if args.eval and args.format_only:
        raise ValueError('--eval and --format_only cannot be both specified')

    if args.out is not None and not args.out.endswith(('.pkl', '.pickle')):
        raise ValueError('The output file must be a pkl file.')

    cfg = Config.fromfile(args.config)
    if args.cfg_options is not None:
        cfg.merge_from_dict(args.cfg_options)
    # import modules from string list.
    if cfg.get('custom_imports', None):
        from mmcv.utils import import_modules_from_strings
        import_modules_from_strings(**cfg['custom_imports'])

    # import modules from plguin/xx, registry will be updated
    if hasattr(cfg, 'plugin'):
        if cfg.plugin:
            import importlib
            if hasattr(cfg, 'plugin_dir'):
                plugin_dir = cfg.plugin_dir
                _module_dir = os.path.dirname(plugin_dir)
                _module_dir = _module_dir.split('/')
                _module_path = _module_dir[0]

                for m in _module_dir[1:]:
                    _module_path = _module_path + '.' + m
                print(_module_path)
                plg_lib = importlib.import_module(_module_path)
            else:
                # import dir is the dirpath for the config file
                _module_dir = os.path.dirname(args.config)
                _module_dir = _module_dir.split('/')
                _module_path = _module_dir[0]
                for m in _module_dir[1:]:
                    _module_path = _module_path + '.' + m
                print(_module_path)
                plg_lib = importlib.import_module(_module_path)

    # set cudnn_benchmark
    if cfg.get('cudnn_benchmark', False):
        torch.backends.cudnn.benchmark = True

    cfg.model.pretrained = None
    # in case the test dataset is concatenated
    samples_per_gpu = 1
    if isinstance(cfg.data.test, dict):
        cfg.data.test.test_mode = True
        samples_per_gpu = cfg.data.test.pop('samples_per_gpu', 1)
        if samples_per_gpu > 1:
            # Replace 'ImageToTensor' to 'DefaultFormatBundle'
            cfg.data.test.pipeline = replace_ImageToTensor(
                cfg.data.test.pipeline)
    elif isinstance(cfg.data.test, list):
        for ds_cfg in cfg.data.test:
            ds_cfg.test_mode = True
        samples_per_gpu = max(
            [ds_cfg.pop('samples_per_gpu', 1) for ds_cfg in cfg.data.test])
        if samples_per_gpu > 1:
            for ds_cfg in cfg.data.test:
                ds_cfg.pipeline = replace_ImageToTensor(ds_cfg.pipeline)

    # init distributed env first, since logger depends on the dist info.
    if args.launcher == 'none':
        distributed = False
    else:
        distributed = True
        init_dist(args.launcher, **cfg.dist_params)

    # set random seeds
    if args.seed is not None:
        set_random_seed(args.seed, deterministic=args.deterministic)

    # build the dataloader
    dataset = build_dataset(cfg.data.test)
    data_loader = build_dataloader(
        dataset,
        samples_per_gpu=samples_per_gpu,
        workers_per_gpu=cfg.data.workers_per_gpu,
        dist=distributed,
        shuffle=False,
        nonshuffler_sampler=cfg.data.nonshuffler_sampler,
    )

    # build the model and load checkpoint
    cfg.model.train_cfg = None
    model = build_model(cfg.model, test_cfg=cfg.get('test_cfg'))
    checkpoint = load_checkpoint(model, args.checkpoint, map_location='cpu')

    # prepare data
    num_frames = 20
    nus_data = nuscenceData(data_loader)
    nus_data.generate_input_data_from_pth(cfg.model, num_frames)

    datas = nus_data.generate_constant_memory_from_pth(cfg.model, num_frames)   # just prepare data, dump constant should be in forward
    # prepare model
    petr_net = PetrWrapper(model)
    petr_net.org_model.to('cuda:0')
    petr_net.org_model.eval()

    for i, data in enumerate(datas):
        petr_net.set_data(data)

        print("*" * 25, "dump frame {} constant".format(i), "*" * 25)
        outs = petr_net()

        out_path = data['dir_path'][0] + '/out/'
        if not os.path.exists(out_path):
            os.mkdir(out_path)
        print("*" * 25, "dump frame {} output&memory".format(i), "*" * 25)

        print("all_cls_scores:", outs['all_cls_scores'].shape)
        outs['all_cls_scores'].cpu().detach().numpy().tofile(out_path + 'all_cls_scores_6x1x428x10_pth.bin')

        print("all_bbox_preds:", outs['all_bbox_preds'].shape)
        outs['all_bbox_preds'].cpu().detach().numpy().tofile(out_path + 'all_bbox_preds_6x1x428x10_pth.bin')

        # memory
        print('mem_embedding:', outs['mem_embedding'].shape)
        outs['mem_embedding'].cpu().detach().numpy().tofile(out_path + 'out_mem_embedding_1x512x256_pth.bin')

        print("mem_timestamp:", outs['mem_timestamp'].shape, outs['mem_timestamp'].dtype)
        outs['mem_timestamp'].cpu().detach().numpy().tofile(out_path + 'out_mem_timestamp_1x512x1_pth.bin')

        print("mem_egopose:", outs['mem_egopose'].shape)
        outs['mem_egopose'].cpu().detach().numpy().tofile(out_path + 'out_mem_egopose_1x512x4x4_pth.bin')

        print("mem_ref_point:", outs['mem_ref_point'].shape)
        outs['mem_ref_point'].cpu().detach().numpy().tofile(out_path + 'out_mem_ref_point_1x512x3_pth.bin')

        print("mem_velo:", outs['mem_velo'].shape)
        outs['mem_velo'].cpu().detach().numpy().tofile(out_path + 'out_mem_velo_1x512x2_pth.bin')

        print("rec_ego_pose:", outs['rec_ego_pose'].shape)
        outs['rec_ego_pose'].cpu().detach().numpy().tofile(out_path + 'out_rec_ego_pose_1x556x4x4_pth.bin')

        print("outs_dec:", outs['outs_dec'].shape)
        outs['outs_dec'].cpu().detach().numpy().tofile(out_path + 'out_outs_dec_1x428x256_pth.bin')

        # post
        print("post_mem_embedding:", outs['post_mem_embedding'].shape)
        outs['post_mem_embedding'].cpu().detach().numpy().tofile(out_path + 'post_mem_embedding_1x640x256_pth.bin')

        print("post_mem_timestamp", outs["post_mem_timestamp"].shape)
        outs["post_mem_timestamp"].cpu().detach().numpy().tofile(out_path + 'post_mem_timestamp_1x640x1_pth.bin')

        print("post_mem_egopose", outs["post_mem_egopose"].shape)
        outs["post_mem_egopose"].cpu().detach().numpy().tofile(out_path + 'post_mem_egopose_1x640x4x4_pth.bin')

        print("post_mem_ref_point", outs["post_mem_ref_point"].shape)
        outs["post_mem_ref_point"].cpu().detach().numpy().tofile(out_path + 'post_mem_ref_point_1x640x3_pth.bin')

        print("post_mem_velo", outs["post_mem_velo"].shape)
        outs["post_mem_velo"].cpu().detach().numpy().tofile(out_path + 'post_mem_velo_1x640x2_pth.bin')
        print("#"*50)
        print(f"Data-{i} done")



if __name__ == '__main__':
    torch.multiprocessing.set_start_method('fork')
    main()