# Copyright (c) Meta Platforms, Inc. and affiliates.

# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.


import math
import torch
import preprocess_data
import shutil
import cv2
import matplotlib.pyplot as plt
import numpy as np
import utils

from typing import Iterable, Optional
from timm.data import Mixup
from timm.utils import accuracy, ModelEma

from tqdm import tqdm
from torchvision import transforms
from PIL import Image
from timm.models import create_model
from timm.data.constants import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD, IMAGENET_INCEPTION_MEAN, IMAGENET_INCEPTION_STD
from pathlib import Path

def train_one_epoch(model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, loss_scaler, max_norm: float = 0,
                    model_ema: Optional[ModelEma] = None, mixup_fn: Optional[Mixup] = None, log_writer=None,
                    wandb_logger=None, start_steps=None, lr_schedule_values=None, wd_schedule_values=None,
                    num_training_steps_per_epoch=None, update_freq=None, use_amp=False):
    model.train(True)
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter('min_lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 10

    optimizer.zero_grad()

    for data_iter_step, (samples, targets) in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        step = data_iter_step // update_freq
        if step >= num_training_steps_per_epoch:
            continue
        it = start_steps + step  # global training iteration
        # Update LR & WD for the first acc
        if lr_schedule_values is not None or wd_schedule_values is not None and data_iter_step % update_freq == 0:
            for i, param_group in enumerate(optimizer.param_groups):
                if lr_schedule_values is not None:
                    param_group["lr"] = lr_schedule_values[it] * param_group["lr_scale"]
                if wd_schedule_values is not None and param_group["weight_decay"] > 0:
                    param_group["weight_decay"] = wd_schedule_values[it]

        samples = samples.to(device, non_blocking=True)
        targets = targets.to(device, non_blocking=True)

        if mixup_fn is not None:
            samples, targets = mixup_fn(samples, targets)

        if use_amp:
            with torch.cuda.amp.autocast():
                output = model(samples)
                loss = criterion(output, targets)
        else: # full precision
            output = model(samples)
            loss = criterion(output, targets)

        loss_value = loss.item()

        if not math.isfinite(loss_value): # this could trigger if using AMP
            print("Loss is {}, stopping training".format(loss_value))
            assert math.isfinite(loss_value)

        if use_amp:
            # this attribute is added by timm on one optimizer (adahessian)
            is_second_order = hasattr(optimizer, 'is_second_order') and optimizer.is_second_order
            loss /= update_freq
            grad_norm = loss_scaler(loss, optimizer, clip_grad=max_norm,
                                    parameters=model.parameters(), create_graph=is_second_order,
                                    update_grad=(data_iter_step + 1) % update_freq == 0)
            if (data_iter_step + 1) % update_freq == 0:
                optimizer.zero_grad()
                if model_ema is not None:
                    model_ema.update(model)
        else: # full precision
            loss /= update_freq
            loss.backward()
            if (data_iter_step + 1) % update_freq == 0:
                optimizer.step()
                optimizer.zero_grad()
                if model_ema is not None:
                    model_ema.update(model)

        torch.cuda.synchronize()

        if mixup_fn is None:
            class_acc = (output.max(-1)[-1] == targets).float().mean()
        else:
            class_acc = None
        metric_logger.update(loss=loss_value)
        metric_logger.update(class_acc=class_acc)
        min_lr = 10.
        max_lr = 0.
        for group in optimizer.param_groups:
            min_lr = min(min_lr, group["lr"])
            max_lr = max(max_lr, group["lr"])

        metric_logger.update(lr=max_lr)
        metric_logger.update(min_lr=min_lr)
        weight_decay_value = None
        for group in optimizer.param_groups:
            if group["weight_decay"] > 0:
                weight_decay_value = group["weight_decay"]
        metric_logger.update(weight_decay=weight_decay_value)
        if use_amp:
            metric_logger.update(grad_norm=grad_norm)

        if log_writer is not None:
            log_writer.update(loss=loss_value, head="loss")
            log_writer.update(class_acc=class_acc, head="loss")
            log_writer.update(lr=max_lr, head="opt")
            log_writer.update(min_lr=min_lr, head="opt")
            log_writer.update(weight_decay=weight_decay_value, head="opt")
            if use_amp:
                log_writer.update(grad_norm=grad_norm, head="opt")
            log_writer.set_step()

        if wandb_logger:
            wandb_logger._wandb.log({
                'Rank-0 Batch Wise/train_loss': loss_value,
                'Rank-0 Batch Wise/train_max_lr': max_lr,
                'Rank-0 Batch Wise/train_min_lr': min_lr
            }, commit=False)
            if class_acc:
                wandb_logger._wandb.log({'Rank-0 Batch Wise/train_class_acc': class_acc}, commit=False)
            if use_amp:
                wandb_logger._wandb.log({'Rank-0 Batch Wise/train_grad_norm': grad_norm}, commit=False)
            wandb_logger._wandb.log({'Rank-0 Batch Wise/global_train_step': it})
            

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}

@torch.no_grad()
def evaluate(data_loader, model, device, use_amp=False):
    criterion = torch.nn.CrossEntropyLoss()

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    import os
    import time
    tm = time.localtime(time.time())
    strtm = time.strftime("%m%d_%I%M%S", tm)
    os.makedirs(f'results/eval_{strtm}/neg', exist_ok=True)
    os.makedirs(f'results/eval_{strtm}/pos', exist_ok=True)

    # switch to evaluation mode
    model.eval()
    
    print(data_loader)
    cnt = 0

    for batch in metric_logger.log_every(data_loader, 10, header):
        images = batch[0]
        path = batch[1]
        bbox = batch[2]
        target = batch[-1]

        images = images.to(device, non_blocking=True)
        images = images.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)

        # compute output
        if use_amp:
            with torch.cuda.amp.autocast():
                output = model(images)
                loss = criterion(output, target)
        else:
            output = model(images)
            loss = criterion(output, target)

        acc1, acc5 = accuracy(output, target, topk=(1, 5))

        batch_size = images.shape[0]
        metric_logger.update(loss=loss.item())
        metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
        metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)

################################################################################ 
        import preprocess_data
        import cv2

        _, predict = torch.max(output, dim=1)
        for t, p, pth, box in zip(target, predict, path, bbox):
            pn='neg' if p==0 else 'pos'
            save_path = f'results/eval_{strtm}/{pn}'
            save_nm = os.path.join(save_path, f't{t}_p{p}_{os.path.basename(pth)}')

            image = cv2.imread(pth)
            x1, y1, x2, y2 =  preprocess_data.xywh2xyxy(box, image.shape[1], image.shape[0])
            # crop_img = image[y1:y2, x1:x2]
            # cv2.imwrite(save_nm, crop_img)
            if p == t:
                image = cv2.rectangle(image,(x1 - 1, y1 - 1),(x2, y2),(0, 0, 255),2)
            else:
                image = cv2.rectangle(image,(x1 - 1, y1 - 1),(x2, y2),(255, 0, 0),1)
            cv2.imwrite(save_nm, image)
            cnt += 1
################################################################################ 

        for class_name, class_id in data_loader.dataset.class_to_idx.items():
            mask = (target == class_id)
            target_class = torch.masked_select(target, mask)
            data_size = target_class.shape[0]
            if data_size > 0:
                mask = mask.unsqueeze(1).expand_as(output)
                output_class = torch.masked_select(output, mask)
                output_class = output_class.view(-1, len(data_loader.dataset.class_to_idx))
                acc1_class, acc5_class = accuracy(output_class, target_class, topk=(1, 5))
                metric_logger.meters[f'acc1_{class_name}'].update(acc1_class.item(), n=data_size)
                metric_logger.meters[f'acc5_{class_name}'].update(acc5_class.item(), n=data_size)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print('* Acc@1 {top1.global_avg:.3f} Acc@5 {top5.global_avg:.3f} loss {losses.global_avg:.3f}'
          .format(top1=metric_logger.acc1, top5=metric_logger.acc5, losses=metric_logger.loss))

    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}

@torch.no_grad()
def prediction(args, device):
    imagenet_default_mean_and_std = args.imagenet_default_mean_and_std
    mean = IMAGENET_INCEPTION_MEAN if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_MEAN
    std = IMAGENET_INCEPTION_STD if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_STD
    totorch = transforms.ToTensor()

    model = create_model(
        args.model, 
        pretrained=False, 
        num_classes=args.nb_classes, 
        drop_path_rate=args.drop_path,
        layer_scale_init_value=args.layer_scale_init_value,
        head_init_scale=args.head_init_scale,
    )
    model.to(device)

    utils.auto_load_model(
        args=args, model=model, model_without_ddp=model,
        optimizer=None, loss_scaler=None, model_ema=None)
    model.eval()
    
    data_list = []
    result = []
    data_root=Path(args.eval_data_path)
    data_list = preprocess_data.make_list(data_root)['data']
    tonorm = transforms.Normalize(mean, std)
    for data in tqdm(data_list, desc='Image Cropping... '):
        crop_img = preprocess_data.crop_image(
            image_path = data[0] / data[1], 
            bbox = data[4], 
            padding = args.padding, 
            padding_size = args.padding_size, 
            use_shift = args.use_shift, 
            use_bbox = args.use_bbox, 
            imsave = args.imsave
        )
        spltnm = str(data[1]).split('_')
        target = int(spltnm[0][1]) if spltnm[0][0] == 't' else -1

        crop_img = cv2.resize(crop_img, (args.input_size, args.input_size))
        crop_img = cv2.cvtColor(crop_img, cv2.COLOR_BGR2RGB)
        pil_image=Image.fromarray(crop_img)
        input_tensor = totorch(pil_image).to(device)
        input_tensor = input_tensor.unsqueeze(dim=0)
        input_tensor = tonorm(input_tensor)
        output_tensor = model(input_tensor)
        
        pred, conf = int(torch.argmax(output_tensor).detach().cpu().numpy()), float((torch.max(output_tensor)).detach().cpu().numpy())
        result.append((pred, conf, target, data[0] / data[1]))
        
    # ##################################### save result image & anno #####################################
    if args.pred_save:
        pos = [x[-1] for x in result if x[0]==1]
        neg = [x[-1] for x in result if x[0]==0]

        for n in tqdm(neg, desc='Negative images copying... '):
            shutil.copy(n, Path(args.pred_save_path) /'negative' / 'images')
            shutil.copy(str(n)[:-3]+'txt', Path(args.pred_save_path) / 'negative' / 'annotations')
        for p in tqdm(pos, desc='Positive images copying... '):
            shutil.copy(p, Path(args.pred_save_path) / 'positive' / 'images')
            shutil.copy(str(p)[:-3]+'txt', Path(args.pred_save_path) / 'positive' / 'annotations')
    # ##################################### save result image & anno #####################################

    ##################################### save evalutations #####################################
    if args.pred_eval:
        if np.sum(np.array(result)[...,2]) < 0:
            conf_TN = [x[1] for x in result if (x[0]==0)]
            conf_TP = [x[1] for x in result if (x[0]==1)]
            conf_FN = []
            conf_FP = []    
            itn = [i for i in range(len(result)) if (result[i][0]==0)]
            itp = [i for i in range(len(result)) if (result[i][0]==1)]

            # histogram P-N 
            plt.hist((conf_TN, conf_TP), label=('Negative', 'Positive'),histtype='bar', bins=50)
            plt.xlabel('Confidence')
            plt.ylabel('Conunt')
            plt.legend(loc='upper left')
            plt.savefig(args.pred_eval_name+'hist_PN.png')
            plt.close()

        else:
            # collect data  
            conf_TN = [x[1] for x in result if (x[0]==x[2] and x[0]==0)]
            conf_TP = [x[1] for x in result if (x[0]==x[2] and x[0]==1)]
            conf_FN = [x[1] for x in result if (x[0]!=x[2] and x[0]==0)]
            conf_FP = [x[1] for x in result if (x[0]!=x[2] and x[0]==1)]
            
            # get index 
            itn = [i for i in range(len(result)) if (result[i][0]==result[i][2] and result[i][0]==0)]
            itp = [i for i in range(len(result)) if (result[i][0]==result[i][2] and result[i][0]==1)]
            ifn = [i for i in range(len(result)) if (result[i][0]!=result[i][2] and result[i][0]==0)]
            ifp = [i for i in range(len(result)) if (result[i][0]!=result[i][2] and result[i][0]==1)]

            # histogram T-F 
            plt.hist(((conf_TN+conf_TP),(conf_FN+conf_FP)), label=('True', 'False'),histtype='bar', bins=50)
            plt.xlabel('Confidence')
            plt.ylabel('Conunt')
            plt.legend(loc='upper left')
            plt.savefig(args.pred_eval_name+'hist_tf.png')
            plt.close()

            # histogram TN TP FN FP
            plt.hist((conf_TN,conf_TP,conf_FN,conf_FP), label=('TN', 'TP','FN','FP'),histtype='bar', bins=30)
            plt.xlabel('Confidence')
            plt.ylabel('Conunt')
            plt.legend(loc='upper left')
            plt.savefig(args.pred_eval_name+'hist_4.png')
            plt.close()
            
        # scatter graph
        if len(conf_TN):
            plt.scatter(conf_TN, itn, alpha=0.4, color='tab:blue', label='TN', s=20)
        if len(conf_TP):
            plt.scatter(conf_TP, itp, alpha=0.4, color='tab:orange', label='TP', s=20)
        if len(conf_FN):
            plt.scatter(conf_FN, ifn, alpha=0.4, color='tab:green', marker='x', label='FN', s=20)
        if len(conf_FP):
            plt.scatter(conf_FP, ifp, alpha=0.4, color='tab:red', marker='x', label='FT', s=20)
        plt.legend(loc='upper right')
        plt.xlabel('Confidence')
        plt.ylabel('Image Index')
        plt.savefig(args.pred_eval_name+'scater.png')
        plt.close()

        # histogram 
        plt.hist(((conf_TN+conf_TP+conf_FN+conf_FP)), histtype='bar', bins=50)
        plt.xlabel('Confidence')
        plt.ylabel('Conunt')
        plt.savefig(args.pred_eval_name+'hist.png')
        plt.close()

    ##################################### save evalutations #####################################
