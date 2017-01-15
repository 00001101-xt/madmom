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

import sys
import argparse
import numpy as np

from madmom.processors import (Processor, IOProcessor, io_arguments,
                               SequentialProcessor, ParallelProcessor)
from madmom.models import PATTERNS_GUITAR, DRUM_PATTERNS
from madmom.features.downbeats import BeatSyncProcessor, GMMBarProcessor
from madmom.drumotron import (DrumotronControlProcessor,
                              DrumotronHardwareProcessor)


def process_online(processor, infile, outfile, **kwargs):
    """
    Process a file or audio stream with the given Processor.

    Parameters
    ----------
    processor : :class:`Processor` instance
        Processor to be processed.
    infile : str or file handle, optional
        Input file (handle). If none is given, the stream present at the
        system's audio inpup is used. Additional keyword arguments can be used
        to influence the frame size and hop size.
    outfile : str or file handle
        Output file (handle).
    kwargs : dict, optional
        Keyword arguments passed to :class:`.audio.signal.Stream` if
        `in_stream` is 'None'.

    """
    while True:
        # process all lines read in via STDIN
        data = infile.readline()
        # parse data 'beat_time feature'
        try:
            beat, feature = data.split(' ')
        except ValueError:
            #
            break
        beat = float(beat)
        feature = float(feature)
        # re-substitue 0 with None
        if beat == 0:
            beat = None
        # do the usual processing
        processor((beat, feature), outfile, **kwargs)


def main():
    """Drumotron controller."""

    # define parser
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description='''
    Drumotron controller.
    ''')
    # version
    p.add_argument('--version', action='version',
                   version='Drumotron.Controller.2016')
    p.add_argument('-a', '--arduino',
                        action='store_true', default=False,
                        help='output to arduino [default=%(default)s]')
    # input/output options
    io_arguments(p, online=True)
    # tracking arguments
    GMMBarProcessor.add_arguments(p)

    # parse arguments
    args = p.parse_args()

    # set immutable arguments
    args.fps = 100
    args.infile = sys.stdin
    args.func = process_online

    # print arguments
    if args.verbose:
        print(args, file=sys.stderr)

    # sync the features to the beats
    beat_sync = BeatSyncProcessor(**vars(args))
    # drum controller
    dhp = DrumotronHardwareProcessor(arduino=args.arduino)
    control_processor = DrumotronControlProcessor(
        DRUM_PATTERNS, delay=5, smooth_win_len=3, out=dhp)
    # score them with a GMM
    gmm_bar_processor = GMMBarProcessor(pattern_files=PATTERNS_GUITAR,
                                        pattern_change_prob=0.001,
                                        out_processor=control_processor,
                                        **vars(args))

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
    processor = IOProcessor([beat_sync, gmm_bar_processor], writer)
    # processor = SequentialProcessor([beat_sync, gmm_bar_processor])

    # and call the processing function
    args.func(processor, **vars(args))


if __name__ == '__main__':
    main()
