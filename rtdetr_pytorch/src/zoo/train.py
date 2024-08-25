import os
import time
import datetime
import json


from torch.cuda.amp import GradScaler
from src.zoo.dataloader import rtdetr_train_dataloader, rtdetr_val_dataloader
from src.zoo.criterion import rtdetr_criterion
from src.data.coco.coco_eval import CocoEvaluator
from src.misc.logger import MetricLogger
from src.solver.det_engine import train_one_epoch
from src.data.coco.coco_utils import get_coco_api_from_dataset
from src.optim.optim import AdamW
from src.optim.ema import ModelEMA

from src.nn.rtdetr.rtdetr_postprocessor import RTDETRPostProcessor
from src.nn.rtdetr import rtdetr
from src.nn.rtdetr.utils import *

import src.misc.dist as dist
import torch.optim.lr_scheduler as lr_schedulers

def fit(model, 
        weight_path, 
        optimizer, 
        save_dir,
        criterion=None,
        train_dataloader=None, 
        val_dataloader=None,
        epoch=72,
        use_amp=True,
        use_ema=True):

    if criterion == None:
        criterion = rtdetr_criterion()
    if train_dataloader == None:
        train_dataloader = rtdetr_train_dataloader()
    if val_dataloader == None:
        val_dataloader = rtdetr_val_dataloader()


    scaler = GradScaler() if use_amp == True else None
    ema_model = ModelEMA(model, decay=0.9999, warmups=2000) if use_ema == True else None
    lr_scheduler = lr_schedulers.MultiStepLR(optimizer=optimizer, milestones=[1000], gamma=0.1) 

    last_epoch = 0
    if weight_path != None:
        last_epoch = load_tuning_state(weight_path, model, ema_model)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    model.to(device)
    ema_model.to(device) if use_ema == True else None
    criterion.to(device)  
    
    #dist wrap modeln loader must do after model.to(device)
    if dist.is_dist_available_and_initialized():
        # model = dist.warp_model(model, find_unused_parameters=True, sync_bn=True)
        # ema_model = dist.warp_model(ema_model, find_unused_parameters=True, sync_bn=True) if use_ema == True else None
        # criterion = dist.warp_model(criterion, find_unused_parameters=True, sync_bn=True)
        train_dataloader = dist.warp_loader(train_dataloader)
        val_dataloader = dist.warp_loader(val_dataloader)
        model = dist.warp_model(model, find_unused_parameters=False, sync_bn=True)

    
    print("Start training")

    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('number of params:', n_parameters)

    start_time = time.time()
    
    for epoch in range(last_epoch + 1, epoch):
        if dist.is_dist_available_and_initialized():
            train_dataloader.sampler.set_epoch(epoch)
        
        train_one_epoch(model, criterion, train_dataloader, optimizer, device, epoch, max_norm=0.1, print_freq=100, ema=ema_model, scaler=scaler)

        lr_scheduler.step()

        dist.save_on_master(state_dict(epoch, model, ema_model), os.path.join(save_dir, f'{epoch}.pth'))

        #TODO eval bug
        # module = ema_model.module if use_ema == True else model
        # test_stats, coco_evaluator = val(model=module, weight_path=None, criterion=criterion, val_dataloader=val_dataloader)

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


@torch.no_grad()
def val(model, weight_path, criterion=None, val_dataloader=None):
    if criterion == None:
        criterion = rtdetr_criterion()
    if val_dataloader == None:
        val_dataloader = rtdetr_val_dataloader()

    model.eval()
    criterion.eval()

    base_ds = get_coco_api_from_dataset(val_dataloader.dataset)
    postprocessor = RTDETRPostProcessor(num_top_queries=300, remap_mscoco_category=val_dataloader.dataset.remap_mscoco_category)
    iou_types = postprocessor.iou_types
    coco_evaluator = CocoEvaluator(base_ds, iou_types)


    if weight_path != None:
        state = torch.hub.load_state_dict_from_url(weight_path, map_location='cpu') if 'http' in weight_path else torch.load(weight_path, map_location='cpu')
        if 'ema' in state:
            model.load_state_dict(state['ema']['module'], strict=False)
        else:
            model.load_state_dict(state['model'], strict=False)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model.to(device)
    criterion.to(device)

    metric_logger = MetricLogger(val_dataloader, header='Test:',)

    panoptic_evaluator = None

    for samples, targets in metric_logger.log_every():
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        outputs = model(samples)

        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)        
        results = postprocessor(outputs, orig_target_sizes)

        res = {target['image_id'].item(): output for target, output in zip(targets, results)}
        if coco_evaluator is not None:
            coco_evaluator.update(res)


    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    if coco_evaluator is not None:
        coco_evaluator.synchronize_between_processes()
    if panoptic_evaluator is not None:
        panoptic_evaluator.synchronize_between_processes()

    if coco_evaluator is not None:
        coco_evaluator.accumulate()
        coco_evaluator.summarize()

    stats = {}

    if coco_evaluator is not None:
        if 'bbox' in iou_types:
            stats['coco_eval_bbox'] = coco_evaluator.coco_eval['bbox'].stats.tolist()
        if 'segm' in iou_types:
            stats['coco_eval_masks'] = coco_evaluator.coco_eval['segm'].stats.tolist()
            
    return stats, coco_evaluator
