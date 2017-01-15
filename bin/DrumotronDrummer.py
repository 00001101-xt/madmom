#!/usr/bin/env python
# encoding: utf-8
"""
Drumotron beat tracking, pattern selection and drum control algorithm.

Drumotron uses different algorithms for different parts:
1. DBNBeatTracker for tracking the beats,
2. GMMBarTracker for tracking the bar and selecting the pattern,
3. logic to control the servos to play a drum kit.

The logic of 1. and 2. is incorporated and modified within this file, since it
needs to run on a Raspberry Pi. However, to be able to do so, the different
parts need to run on different CPU cores, since the Pi has rather limited
processing power.

"""

from __future__ import absolute_import, division, print_function

import argparse
import multiprocessing as mp
from functools import partial

import numpy as np

from madmom.processors import (IOProcessor, io_arguments, SequentialProcessor,
                               ParallelProcessor)
from madmom.audio.signal import SignalProcessor, FramedSignalProcessor
from madmom.audio.stft import ShortTimeFourierTransformProcessor
from madmom.audio.spectrogram import (FilteredSpectrogramProcessor,
                                      LogarithmicSpectrogramProcessor,
                                      SpectrogramDifferenceProcessor)
from madmom.ml.nn import NeuralNetwork, NeuralNetworkEnsemble
from madmom.models import BEATS_LSTM, PATTERNS_GUITAR, DRUM_PATTERNS
from madmom.features.beats import DBNBeatTrackingProcessor
from madmom.features.downbeats import BeatSyncProcessor, GMMBarProcessor
from madmom.drumotron import DrumotronControlProcessor
from madmom.drumotron import DrumotronHardwareProcessor


def main():
    """Drumotron"""

    # define parser
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description='''
    DrumotronDrummer
    ''')
    # version
    p.add_argument('--version', action='version',
                   version='Drumotron.2016')
    # input/output options
    io_arguments(p, output_suffix=None, online=True)
    # signal processing arguments
    SignalProcessor.add_arguments(p, norm=False, gain=0)
    # tracking arguments
    DBNBeatTrackingProcessor.add_arguments(p, min_bpm=70, max_bpm=150)
    GMMBarProcessor.add_arguments(p)

    # parse arguments
    args = p.parse_args()

    # set immutable arguments
    args.fps = 100

    # print arguments
    if args.verbose:
        print(args)

    # define signal processing used by all others
    sig = SignalProcessor(num_channels=1, sample_rate=44100)
    frames = FramedSignalProcessor(frame_size=2048, **vars(args))
    stft = ShortTimeFourierTransformProcessor()  # caching FFT window
    sig_proc = SequentialProcessor((sig, frames, stft))

    # here, the processing for the RNN & GMM diverges

    # beat tracking processor
    filt = FilteredSpectrogramProcessor(num_bands=12, fmin=30,
                                        fmax=17000, norm_filters=True)
    spec = LogarithmicSpectrogramProcessor(mul=1, add=1)
    diff = SpectrogramDifferenceProcessor(diff_ratio=0.5, positive_diffs=True,
                                          stack_diffs=np.hstack)
    # nn = NeuralNetworkEnsemble.load(BEATS_LSTM)
    nn = NeuralNetwork.load(BEATS_LSTM[3])
    dbn = DBNBeatTrackingProcessor(**vars(args))
    beat_processor = SequentialProcessor((filt, spec, diff, nn, dbn))

    # gmm feature
    filt = FilteredSpectrogramProcessor(num_bands=12, fmin=60,
                                        fmax=17000, norm_filters=True)
    spec = LogarithmicSpectrogramProcessor(mul=1, add=1)
    diff = SpectrogramDifferenceProcessor(diff_ratio=0.5, positive_diffs=True)
    agg = partial(np.sum, axis=1)
    gmm_feat_processor = SequentialProcessor((filt, spec, diff, agg))

    # extract beat & gmm feature in parallel
    beat_downbeat_processor = ParallelProcessor((beat_processor,
                                                 gmm_feat_processor))

    # sync the features to the beats
    beat_sync = BeatSyncProcessor(**vars(args))
    # score them with a GMM
    gmm_bar_processor = GMMBarProcessor(pattern_files=PATTERNS_GUITAR,
                                        pattern_change_prob=0.001,
                                        **vars(args))
    dhp = DrumotronHardwareProcessor()
    control_processor = DrumotronControlProcessor(
        DRUM_PATTERNS, delay=0, smooth_win_len=5, out=dhp)

    # output handler
    if args.online:
        # simply output the given string
        from madmom.utils import write_output as writer
    elif args.downbeats:
        # simply write the timestamps of the downbeats
        from madmom.utils import write_events as writer
    else:
        # borrow the note writer for outputting timestamps + beat numbers
        from madmom.features.notes import write_notes as writer

    # create an IOProcessor
    processor = IOProcessor([sig_proc, beat_downbeat_processor, beat_sync,
                             gmm_bar_processor, control_processor], writer)

    # and call the processing function
    args.func(processor, **vars(args))


if __name__ == '__main__':
    main()
