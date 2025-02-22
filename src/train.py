from argparse import ArgumentParser
import os
import inspect
from glob import glob
from pathlib import Path
import json
import hashlib
import pickle

import torch
import torch.multiprocessing

from fastspeech2.fastspeech2 import FastSpeech2
from pytorch_lightning import Trainer
from pytorch_lightning.tuner.tuning import Tuner
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.callbacks import LearningRateMonitor
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
import matplotlib.pyplot as plt

from alignments.datasets.libritts import LibrittsDataset
from third_party.argutils import str2bool

torch.multiprocessing.set_sharing_strategy("file_system")

if __name__ == "__main__":
    parser = ArgumentParser()

    parser = Trainer.add_argparse_args(parser)

    parser.add_argument("--early_stopping", type=str2bool, default=True)
    parser.add_argument("--early_stopping_patience", type=int, default=4)

    lr_monitor = LearningRateMonitor(logging_interval="step")
    callbacks = [lr_monitor]

    parser.add_argument("--dataset_cache_path", type=str, default="../dataset_cache")
    parser.add_argument("--no_cache", type=str2bool, default=False)

    parser.add_argument(
        "--train_target_path", type=str, nargs="+", default=["../data/train-clean-360-aligned"]
    )
    parser.add_argument(
        "--train_source_path", type=str, nargs="+", default=["../data/train-clean-360"]
    )
    parser.add_argument(
        "--train_source_url",
        type=str,
        nargs="+",
        default=["https://www.openslr.org/resources/60/train-clean-360.tar.gz"],
    )
    parser.add_argument("--train_tmp_path", type=str, default="../tmp")

    parser.add_argument(
        "--valid_target_path", type=str, default="../data/dev-clean-aligned"
    )
    parser.add_argument("--valid_source_path", type=str, default="../data/dev-clean")
    parser.add_argument(
        "--valid_source_url",
        type=str,
        default="https://www.openslr.org/resources/60/dev-clean.tar.gz",
    )
    parser.add_argument("--valid_tmp_path", type=str, default="../tmp")

    parser = FastSpeech2.add_model_specific_args(parser)
    parser = FastSpeech2.add_dataset_specific_args(parser)

    parser.add_argument("--wandb_project", type=str, default="fastspeech2")
    parser.add_argument("--wandb_mode", type=str, default="online")
    parser.add_argument("--wandb_name", type=str, default=None)
    parser.add_argument("--checkpoint", type=str2bool, default=True)
    parser.add_argument("--checkpoint_key", type=str, default="eval/mel_loss")
    parser.add_argument("--checkpoint_mode", type=str, default="min")
    parser.add_argument("--checkpoint_path", type=str, default="models")
    parser.add_argument("--checkpoint_filename", type=str, default=None)
    parser.add_argument("--from_checkpoint", type=str, default=None)

    parser.add_argument("--visible_gpus", type=int, default=0)

    args = parser.parse_args()
    var_args = vars(args)

    os.environ["WANDB_MODE"] = var_args["wandb_mode"]
    if var_args["wandb_name"] is None:
        wandb_logger = WandbLogger(project=var_args["wandb_project"])
    else:
        wandb_logger = WandbLogger(
            project=var_args["wandb_project"], name=var_args["wandb_name"]
        )

    train_ds = []

    train_ds_kwargs = {
        k.replace("train_", ""): v
        for k, v in var_args.items()
        if k.startswith("train_")
    }

    valid_ds_kwargs = {
        k.replace("valid_", ""): v
        for k, v in var_args.items()
        if k.startswith("valid_")
    }

    if not var_args["no_cache"]:
        Path(var_args["dataset_cache_path"]).mkdir(parents=True, exist_ok=True)
        cache_path = Path(var_args["dataset_cache_path"])
    else:
        cache_path = None

    for i in range(len(var_args["train_target_path"])):
        if not var_args["no_cache"]:
            kwargs = train_ds_kwargs
            kwargs.update({"target_directory": var_args["train_target_path"][i]})
            ds_hash = hashlib.md5(json.dumps(kwargs, sort_keys=True).encode('utf-8')).hexdigest()
            cache_path_alignments = Path(var_args["dataset_cache_path"]) / f"train-alignments-{ds_hash}.pt"
        if len(list(Path(var_args["train_target_path"][i]).rglob("**/*.TextGrid"))) == 0 or not cache_path_alignments.exists() or var_args["no_cache"]:
            train_ds += [LibrittsDataset(
                target_directory=var_args["train_target_path"][i],
                source_directory=var_args["train_source_path"][i],
                source_url=var_args["train_source_url"][i],
                verbose=True,
                tmp_directory=var_args["train_tmp_path"],
                chunk_size=10_000,
            )]
            if not var_args["no_cache"]:
                train_ds[-1].hash = ds_hash
                with open(cache_path_alignments, "wb") as f:
                    pickle.dump(train_ds[-1], f)
        else:
            if cache_path_alignments.exists():
                with open(cache_path_alignments, "rb") as f:
                    train_ds += [pickle.load(f)]
                
    if not var_args["no_cache"]:
        kwargs = valid_ds_kwargs
        kwargs.update({"target_directory": var_args["valid_target_path"]})
        ds_hash = hashlib.md5(json.dumps(kwargs, sort_keys=True).encode('utf-8')).hexdigest()
        cache_path_alignments = Path(var_args["dataset_cache_path"]) / f"valid-alignments-{ds_hash}.pt"
    if len(list(Path(var_args["valid_target_path"]).rglob("**/*.TextGrid"))) == 0 or not cache_path_alignments.exists():
        valid_ds = LibrittsDataset(
            target_directory=var_args["valid_target_path"],
            source_directory=var_args["valid_source_path"],
            source_url=var_args["valid_source_url"],
            verbose=True,
            tmp_directory=var_args["valid_tmp_path"],
            chunk_size=10_000,
        )
        if not var_args["no_cache"]:
            valid_ds.hash = ds_hash
            with open(cache_path_alignments, "wb") as f:
                pickle.dump(valid_ds, f)
    else:
        if cache_path_alignments.exists():
            with open(cache_path_alignments, "rb") as f:
                valid_ds = pickle.load(f)

    model_args = {
        k: v
        for k, v in var_args.items()
        if k in inspect.signature(FastSpeech2).parameters
    }

    del var_args["train_target_path"]
    del var_args["train_source_path"]
    del var_args["train_source_url"]
    del var_args["train_tmp_path"]
    del var_args["valid_target_path"]
    del var_args["valid_source_path"]
    del var_args["valid_source_url"]
    del var_args["valid_nexamples"]
    del var_args["valid_example_directory"]
    del var_args["valid_tmp_path"]

    if args.from_checkpoint is not None:
        model = FastSpeech2.load_from_checkpoint(
            args.from_checkpoint,
            train_ds=train_ds,
            valid_ds=valid_ds,
            train_ds_kwargs=train_ds_kwargs,
            valid_ds_kwargs=valid_ds_kwargs,
            num_workers=4,
            batch_size=args.batch_size,
            cache_path=cache_path,
        )
    else:
        model_args["cache_path"] = cache_path
        model = FastSpeech2(
            train_ds,
            valid_ds,
            train_ds_kwargs={
                k.replace("train_", ""): v
                for k, v in var_args.items()
                if k.startswith("train_")
            },
            valid_ds_kwargs={
                k.replace("valid_", ""): v
                for k, v in var_args.items()
                if k.startswith("valid_")
            },
            **model_args,
        )

    if var_args["checkpoint_filename"] is None and var_args["wandb_name"] is not None:
        var_args["checkpoint_filename"] = var_args["wandb_name"]

    if var_args["checkpoint"]:
        callbacks.append(
            ModelCheckpoint(
                monitor=var_args["checkpoint_key"],
                mode=var_args["checkpoint_mode"],
                filename=var_args["checkpoint_filename"],
                dirpath=var_args["checkpoint_path"],
            )
        )

    if var_args["early_stopping"]:
        callbacks.append(
            EarlyStopping(
                monitor="eval/mel_loss", patience=var_args["early_stopping_patience"]
            )
        )

    trainer = Trainer.from_argparse_args(
        args,
        callbacks=callbacks,
        default_root_dir="logs",
        logger=wandb_logger,
    )

    trainer.fit(model)
