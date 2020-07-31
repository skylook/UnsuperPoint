import torch
import os
import glob
import tqdm
from torch.nn.utils import clip_grad_norm_
import torch.optim as optim
import torch.optim.lr_scheduler as lr_sched
import torchvision
import numpy as np
import matplotlib.pyplot as plt

names = ['usp', 'uni_xy', 'desc', 'decorr', 'des_key', 'peaky']
colors = ['black', 'orange', 'red', 'red', 'blue', 'green']
linestyles = ['-', '-', '-', '--', '-', '-']

def build_optimizer(model, optim_config):
    if optim_config['name'] == 'adam':
        optimizer = optim.Adam(model.parameters(), lr=optim_config['LR'], weight_decay=optim_config['weight_decay'])
    elif optim_config['name'] == 'sgd':
        optimizer = optim.SGD(
            model.parameters(), lr=optim_config['LR'], weight_decay=optim_config['weight_decay'],
            momentum=optim_config['momentum']
        )
    else:
        raise NotImplementedError
    return optimizer

def build_scheduler(optimizer, total_iters_each_epoch, total_epochs, last_epoch, optim_cfg):
    decay_epochs = [x * total_epochs for x in optim_cfg['decay_step_list']]
    def lr_lbmd(cur_epoch):
        cur_decay = 1
        for decay_epoch in decay_epochs:
            if cur_epoch >= decay_epoch:
                cur_decay = cur_decay * optim_cfg['LR_decay']
        return max(cur_decay, optim_cfg['LR_clip'] / optim_cfg['LR'])

    lr_warmup_scheduler = None
    total_steps = total_iters_each_epoch * total_epochs
    if optim_cfg['name'] == 'adam_onecycle':
        lr_scheduler = OneCycle(
            optimizer, total_steps, optim_cfg['LR'], list(optim_cfg['MOMS']), optim_cfg['div_factors'], optim_cfg['pct_start']
        )
    else:
        lr_scheduler = lr_sched.LambdaLR(optimizer, lr_lbmd, last_epoch=last_epoch)

        if optim_cfg['LR_warmup']:
            lr_warmup_scheduler = CosineWarmupLR(
                optimizer, T_max=optim_cfg['WARMUP_EPOCH'] * len(total_iters_each_epoch),
                eta_min=optim_cfg['LR'] / optim_cfg['div_factors']
            )

    return lr_scheduler, lr_warmup_scheduler

def train_one_epoch(model, optimizer, train_loader, lr_scheduler, accumulated_iter, optim_cfg,
                    rank, tbar, total_it_each_epoch, dataloader_iter, tb_log=None, leave_pbar=False):
    if total_it_each_epoch == len(train_loader):
        dataloader_iter = iter(train_loader)

    if rank == 0:
        pbar = tqdm.tqdm(total=total_it_each_epoch, leave=leave_pbar, desc='train', dynamic_ncols=True)

    disp_dict = {}
    for cur_it in range(total_it_each_epoch):
        try:
            img0, img1, mat = next(dataloader_iter)
        except StopIteration:
            dataloader_iter = iter(train_loader)
            img0, img1, mat = next(dataloader_iter)
            print('new iters')

        if rank == 0 and cur_it == 0:
            img0_grid = torchvision.utils.make_grid(img0)
            img1_grid = torchvision.utils.make_grid(img1)

            tb_log.add_image('original_images', img0_grid)
            tb_log.add_image('homography', img1_grid)
        
        img0 = img0.cuda()
        img1 = img1.cuda()
        mat = mat.cuda()

        try:
            cur_lr = float(optimizer.lr)
        except:
            cur_lr = optimizer.param_groups[0]['lr']

        if tb_log is not None:
            tb_log.add_scalar('learning_rate', cur_lr, accumulated_iter)

        
        model.train()
        optimizer.zero_grad()

        loss, loss_item = model(img0, img1, mat)

        loss.backward()
        total_norm = clip_grad_norm_(model.parameters(), optim_cfg['GRAD_NORM_CLIP'])
        optimizer.step()
        
        accumulated_iter += 1
        disp_dict.update({'loss': loss.item(), 'lr': cur_lr, 'grad_norm':total_norm})

        # log to console and tensorboard
        if rank == 0:
            pbar.update()
            pbar.set_postfix(dict(total_it=accumulated_iter))
            tbar.set_postfix(disp_dict)
            tbar.refresh()

            if tb_log is not None:
                tb_log.add_scalar('train_loss', loss, accumulated_iter)
                tb_log.add_scalar('learning_rate', cur_lr, accumulated_iter)
                tb_log.add_scalar('key_dist_loss', loss_item[0], accumulated_iter)
                tb_log.add_scalar('Uni_xy', loss_item[1], accumulated_iter)
                tb_log.add_scalar('desc_loss', loss_item[2], accumulated_iter)
                tb_log.add_scalar('decoor_loss', loss_item[3], accumulated_iter)
                tb_log.add_scalar('key_dist', loss_item[4], accumulated_iter)
    lr_scheduler.step()
    if rank == 0:
        pbar.close()
    return accumulated_iter


def train_model(model, optimizer, train_loader, lr_scheduler, optim_cfg,
                start_epoch, total_epochs, start_iter, rank, tb_log, ckpt_save_dir, train_sampler=None,
                lr_warmup_scheduler=None, ckpt_save_interval=1, max_ckpt_save_num=50):
    accumulated_iter = start_iter
    with tqdm.trange(start_epoch, total_epochs, desc='epochs', dynamic_ncols=True, leave=(rank == 0)) as tbar:
        total_it_each_epoch = len(train_loader)
        dataloader_iter = iter(train_loader)
        for cur_epoch in tbar:
            if train_sampler is not None:
                train_sampler.set_epoch(cur_epoch)

            # train one epoch
            if lr_warmup_scheduler is not None and cur_epoch < optim_cfg.WARMUP_EPOCH:
                cur_scheduler = lr_warmup_scheduler
            else:
                cur_scheduler = lr_scheduler
            accumulated_iter = train_one_epoch(
                model, optimizer, train_loader,
                lr_scheduler=cur_scheduler,
                accumulated_iter=accumulated_iter, optim_cfg=optim_cfg,
                rank=rank, tbar=tbar, tb_log=tb_log,
                leave_pbar=(cur_epoch + 1 == total_epochs),
                total_it_each_epoch=total_it_each_epoch,
                dataloader_iter=dataloader_iter
            )

            # save trained model
            trained_epoch = cur_epoch + 1
            if trained_epoch % ckpt_save_interval == 0 and rank == 0:

                ckpt_list = glob.glob(str(ckpt_save_dir / 'checkpoint_epoch_*.pth'))
                ckpt_list.sort(key=os.path.getmtime)

                if ckpt_list.__len__() >= max_ckpt_save_num:
                    for cur_file_idx in range(0, len(ckpt_list) - max_ckpt_save_num + 1):
                        os.remove(ckpt_list[cur_file_idx])

                ckpt_name = ckpt_save_dir / ('checkpoint_epoch_%d' % trained_epoch)
                save_checkpoint(
                    checkpoint_state(model, optimizer, trained_epoch, accumulated_iter), filename=ckpt_name,
                )

def model_state_to_cpu(model_state):
    model_state_cpu = type(model_state)()  # ordered dict
    for key, val in model_state.items():
        model_state_cpu[key] = val.cpu()
    return model_state_cpu

def checkpoint_state(model=None, optimizer=None, epoch=None, it=None):
    optim_state = optimizer.state_dict() if optimizer is not None else None
    if model is not None:
        if isinstance(model, torch.nn.parallel.DistributedDataParallel):
            model_state = model_state_to_cpu(model.module.state_dict())
        else:
            model_state = model.state_dict()
    else:
        model_state = None

    return {'epoch': epoch, 'it': it, 'model_state': model_state, 'optimizer_state': optim_state}


def save_checkpoint(state, filename='checkpoint'):
    if False and 'optimizer_state' in state:
        optimizer_state = state['optimizer_state']
        state.pop('optimizer_state', None)
        optimizer_filename = '{}_optim.pth'.format(filename)
        torch.save({'optimizer_state': optimizer_state}, optimizer_filename)

    filename = '{}.pth'.format(filename)
    torch.save(state, filename)

if __name__=='__main__':
    # test scheduler

    import yaml
    cfg = None
    with open('./Unsuper/configs/UnsuperPoint_coco.yaml', 'r') as f:
        cfg = yaml.load(f, Loader=yaml.FullLoader)
    import torch.nn as nn
    x = nn.Linear(3, 1)
    opt = optim.SGD(x.parameters(), lr=0.001)
    sch, _ = build_scheduler(opt, 10, 80, -1, cfg['MODEL']['OPTIMIZATION'])

    for i in range(80):
        sch.step()
        print(opt.param_groups[0]['lr'])


