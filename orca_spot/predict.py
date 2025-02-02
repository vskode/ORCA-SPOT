#!/usr/bin/env python3

"""
Module: predict.py
Authors: Christian Bergler, Hendrik Schroeter
License: GNU General Public License v3.0
Institution: Friedrich-Alexander-University Erlangen-Nuremberg, Department of Computer Science, Pattern Recognition Lab
Last Access: 26.04.2022
"""
#%%
import argparse

import torch
import torch.nn as nn

from math import ceil, floor
from utils.logging import Logger
from collections import OrderedDict
from models.classifier import DefaultClassifierOpts, Classifier
from data.audiodataset import DefaultSpecDatasetOps, StridedAudioDataset
from models.residual_encoder import DefaultEncoderOpts, ResidualEncoder as Encoder
#%%
parser = argparse.ArgumentParser()

parser.add_argument(
    "-d",
    "--debug",
    dest="debug",
    action="store_true",
    help="Print additional training and model information.",
)

parser.add_argument(
    "--model_path",
    type=str,
    default=None,
    help="Path to a model.",
)

parser.add_argument(
    "--checkpoint_path",
    type=str,
    default=None,
    help="Path to a checkpoint. "
    "If provided the checkpoint will be used instead of the model.",
)

parser.add_argument(
    "--log_dir", type=str, default=None, help="The directory to store the logs."
)

parser.add_argument(
    "--sequence_len", type=float, default=2, help="Sequence length in [s]."
)

parser.add_argument(
    "--hop", type=float, default=1, help="Hop [s] of subsequent sequences."
)

parser.add_argument(
    "--threshold",
    type=float,
    default=0.5,
    help="Threshold for the probability for detecting an orca.",
)

parser.add_argument(
    "--batch_size", type=int, default=1, help="The number of images per batch."
)

parser.add_argument(
    "--num_workers", type=int, default=4, help="Number of workers used in data-loading"
)

parser.add_argument(
    "--jit_load",
    dest="jit_load",
    action="store_true",
    help="Load model via torch jit (otherwise via torch load).",
)

parser.add_argument(
    "--min_max_norm",
    dest="min_max_norm",
    action="store_true",
    help="activates min-max normalization instead of default 0/1-dB-normalization.",
)

parser.add_argument(
    "--no_cuda",
    dest="cuda",
    action="store_false",
    help="Do not use cuda to train model.",
)

parser.add_argument(
    "audio_files", type=str, nargs="+", help="Audio file to predict the call locations"
)


ARGS = parser.parse_args()

log = Logger("PREDICT", ARGS.debug, ARGS.log_dir)

models = {"encoder":1, "classifier":2}

"""
Main function to compute prediction (segmentation) by using a trained model together with a given audio tape by processing a sliding window approach
"""
if __name__ == "__main__":
    if ARGS.checkpoint_path is not None:
        log.info(
            "Restoring checkpoint from {} instead of using a model file.".format(
                ARGS.checkpoint_path
            )
        )
        checkpoint = torch.load(ARGS.checkpoint_path, map_location="cpu")
        encoder = Encoder(DefaultEncoderOpts)
        classifier = Classifier(DefaultClassifierOpts)
        model = nn.Sequential(
            OrderedDict([("encoder", encoder), ("classifier", classifier)])
        )
        model.load_state_dict(checkpoint["modelState"])
        log.warning(
            "Using default preprocessing options. Provide Model file if they are changed"
        )
        dataOpts = DefaultSpecDatasetOps
    else:
        if ARGS.jit_load:
            extra_files = {}
            extra_files['dataOpts'] = ''
            extra_files['encoderOpts'] = ''
            extra_files['classifierOpts'] = ''
            model = torch.jit.load(ARGS.model_path, _extra_files=extra_files)
            encoder = model.encoder
            classifier = model.classifier
            encoderState = encoder.state_dict()
            classifierState = classifier.state_dict()
            dataOpts = eval(extra_files['dataOpts'])
            encoderOpts = eval(extra_files['encoderOpts'])
            classifierOpts = eval(extra_files['classifierOpts'])
        else:
            model_dict = torch.load(ARGS.model_path)
            dataOpts = model_dict["dataOpts"]
            encoder = Encoder(model_dict["encoderOpts"])
            encoder.load_state_dict(model_dict["encoderState"])
            classifier = Classifier(model_dict["classifierOpts"])
            classifier.load_state_dict(model_dict["classifierState"])
            model = nn.Sequential(
                OrderedDict([("encoder", encoder), ("classifier", classifier)])
            )


    log.info(model)

    if torch.cuda.is_available() and ARGS.cuda:
        model = model.cuda()
    model.eval()

    sr = dataOpts['sr']
    hop_length = dataOpts["hop_length"]
    n_fft = dataOpts["n_fft"]

    try:
        n_freq_bins = dataOpts["num_mels"]
    except KeyError:
        n_freq_bins = dataOpts["n_freq_bins"]

    fmin = dataOpts["fmin"]
    fmax = dataOpts["fmax"]
    freq_cmpr = dataOpts["freq_compression"]
    DefaultSpecDatasetOps["min_level_db"] = dataOpts["min_level_db"]
    DefaultSpecDatasetOps["ref_level_db"] = dataOpts["ref_level_db"]

    log.debug("dataOpts: " + str(dataOpts))

    if ARGS.min_max_norm:
        log.debug("Init min-max-normalization activated")
    else:
        log.debug("Init 0/1-dB-normalization activated")

    sequence_len = int(ceil(ARGS.sequence_len * sr))
    hop = int(ceil(ARGS.hop * sr))

    log.info("Predicting {} files".format(len(ARGS.audio_files)))

    for file_name in ARGS.audio_files:
        log.info(file_name)
        dataset = StridedAudioDataset(
            file_name.strip(),
            sequence_len=sequence_len,
            hop=hop,
            sr=sr,
            fft_size=n_fft,
            fft_hop=hop_length,
            n_freq_bins=n_freq_bins,
            freq_compression=freq_cmpr,
            f_min=fmin,
            f_max=fmax,
            min_max_normalize=ARGS.min_max_norm
        )
        data_loader = torch.utils.data.DataLoader(
            dataset,
            batch_size=ARGS.batch_size,
            num_workers=ARGS.num_workers,
            pin_memory=True,
        )

        log.info("size of the file(samples)={}".format(dataset.n_frames))
        log.info("size of hop(samples)={}".format(hop))
        stop = int(max(floor(dataset.n_frames / hop), 1))
        log.info("stop time={}".format(stop))

        with torch.no_grad():
            for i, input in enumerate(data_loader):
                if torch.cuda.is_available() and ARGS.cuda:
                    input = input.cuda()
                out = model(input).cpu()

                for n in range(out.shape[0]):
                    t_start = (i * ARGS.batch_size + n) * hop
                    t_end = min(t_start + sequence_len - 1, dataset.n_frames - 1)
                    log.debug("start extract={}".format(t_start))
                    log.debug("end extract={}".format(t_end))
                    prob = torch.nn.functional.softmax(out, dim=1).numpy()[n, 1]
                    pred = int(prob >= ARGS.threshold)
                    log.info(
                        "time={}-{}, pred={}, prob={}".format(
                            round(t_start / sr, 2), round(t_end / sr, 2), pred, prob
                        )
                    )
        log.debug("Finished proccessing")

    log.close()
