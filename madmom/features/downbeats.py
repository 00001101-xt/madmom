# encoding: utf-8
"""
This file contains downbeat tracking related functionality.

"""

from __future__ import absolute_import, division, print_function

import warnings
import pickle
import os.path

import numpy as np

from madmom.utils import match_file
from madmom.processors import Processor, SequentialProcessor
from madmom.features.beats_hmm import RNNBeatTrackingObservationModel
from madmom.ml.hmm import ObservationModel
from madmom.utils import search_files, load_events


class BeatSyncProcessor(Processor):
    """
    Synchronize features to the beat.

    """
    def __init__(self, beat_subdivisions, feat_dim=1, fps=100, online=False,
                 **kwargs):
        self.beat_subdivisions = beat_subdivisions
        # FIXME: feat_dim must be determined automatically
        self.feat_dim = feat_dim
        self.fps = fps
        self.online = online
        # sum up the feature values of one beat division
        # FIXME: does not work with arrays, only for scalars
        self.feat_sum = 0.
        # count the frames since last beat division
        self.counter = 0
        self.current_div = 0
        # length of one beat division in audio frames (depends on tempo)
        self.div_frames = None
        # store last beat time to compute tempo
        self.last_beat = None
        self.beat_features = np.zeros((beat_subdivisions, feat_dim))
        # offset the beat subdivision borders
        self.offset = 2

    def process(self, data, **kwargs):
        """
        Syncronize features to the beats.

        Parameters
        ----------
        data : tuple (beat_time(s), feature(s))

        Returns
        -------
        beat_times : None or float, or numpy array
            Beat time.
        beat_features : numpy array, shape (1, beat_subdivisions, feat_dim)
            Beat synchronous features.

        Notes
        -----
        Depending on online/offline mode the beats and features are either
        syncronized on a frame-by-frame basis or for the whole sequence,
        respectively.

        """
        if self.online:
            return self.process_online(data, **kwargs)
        else:
            return self.process_offline(data, **kwargs)

    def process_online(self, data, **kwargs):
        """
        This function organises the features of a piece according to the
        given beats. First, a beat interval is divided into <beat_subdivision>
        divisions. Then all features that fall into one subdivision are
        averaged. If no feature value for a subdivision is found, it is
        interpolated.

        Parameters
        ----------
        data : tuple (beat_time, feature)

        Returns
        -------
        beat_time : None or float
            Beat time (or None if no beat is present).
        beat_feat : numpy array, shape (1, beat_subdivisions, feat_dim)
            Beat synchronous features (or None if no beat is present)

        """
        beat, feature = data
        is_beat = beat.any()
        beat_feat = np.empty((0, 3))

        # init before first beat:
        if self.last_beat is None:
            if is_beat:
                # store last_beat_time
                self.last_beat = beat
            # don't return anything, since beat_feat is not synchronised yet
            return np.empty(0), beat_feat

        # init before second beat:
        if self.div_frames is None:
            if is_beat:
                # set tempo (div_frames)
                beat_interval = beat - self.last_beat
                self.div_frames = np.diff(np.round(
                    np.linspace(0, beat_interval * self.fps,
                                self.beat_subdivisions + 1)))
                self.last_beat = beat
            # don't return anything, since beat_feat is not synchronised yet
            return np.empty(0), beat_feat

        # normal action, everything is initialised
        # add feature to the cumulative sum
        self.feat_sum += feature
        self.counter += 1  # starts with 1
        # check if the current frame is the end of a beat subdivision
        is_end_div = (self.counter >= self.div_frames[self.current_div])
        if is_end_div:
            # compute mean of the features in the previous subdivision
            self.beat_features[self.current_div, :] = \
                self.feat_sum / self.counter
            # proceed to the next subdivision
            self.current_div = (self.current_div + 1) % self.beat_subdivisions
            # reset cumulative sum and the frame counter
            self.feat_sum = 0.
            self.counter = 0
        if is_beat:
            beat_feat = self.beat_features[np.newaxis, :]
            # compute new beat interval (tempo)
            beat_interval = beat - self.last_beat
            # update beat subdivision lengths
            self.div_frames = np.diff(np.round(np.linspace(
                0, beat_interval * self.fps, self.beat_subdivisions + 1)))
            # If we reset the frame_counter, we also have to modify feat_sum
            if self.counter > self.offset:
                self.feat_sum = self.feat_sum * \
                                self.offset / self.counter
            else:
                self.feat_sum = self.beat_features[-1, :] * self.offset
            # Reset frame counter. If we want to collect features before the
            # actual subdivision borders, we start counting with an offset
            self.counter = self.offset
            # store last beat time
            self.last_beat = beat
            self.current_div = 0
            # remove old entries
            self.beat_features = np.zeros((self.beat_subdivisions,
                                           self.feat_dim))
        return beat, beat_feat

    def process_offline(self, data, **kwargs):
        """
        This function organises the features of one song according to the
        given beats. First, a beat interval is divided into <beat_subdivision>
        divisions. Then all features that fall into one subdivision are
        summarised by a <summarise> function. If no feature value for a
        subdivision is found, it is interpolated.

        Parameters
        ----------
        data : tuple (beat_times, features)
            Tuple of two numpy arrays, the first containing the beat times in
            seconds, the second the features to be synchronised to them.

        Returns
        -------
        beat_times : numpy array
            Beat times.
        beat_features : numpy array [1, beat_subdivisions, feat_dim]
            Beat synchronous features.

        """
        beats, features = data
        # no beats, return immediately
        if beats.size == 0:
            return np.array([]), np.array([])
        # beats can be 1D (only beat times) or 2D (times, position inside bar)
        if beats.ndim == 1:
            beats = np.atleast_2d(beats).T

        while (float(len(features)) / self.fps) < beats[-1, 0]:
            beats = beats[:-1, :]
            warnings.warn('Beat sequence too long compared to features.')

        if features.ndim > 1:
            feat_dim = features.shape[1]
        else:
            # last singleton dim is deleted by activations.load, re-add it
            # FIXME: use np.array(ndmin=2, copy=False)?
            warnings.warn('FIXME: feature loading')
            features = features[:, np.newaxis]
            feat_dim = 1

        num_beats = len(beats)

        # init a 3D feature aggregation array
        beat_features = np.empty((num_beats - 1, self.beat_subdivisions,
                                  feat_dim))
        # allow the first beat to be 20 ms too early
        first_next_beat_frame = int(
            np.max([0, np.floor((beats[0, 0] - 0.02) * self.fps)]))
        for i_beat in range(num_beats - 1):
            duration_beat = beats[i_beat + 1, 0] - beats[i_beat, 0]
            # gmms should be centered on the annotations and cover a timespan
            # that is duration_bar/num_gmms_per_bar. Subtract half of this
            # timespan to get the start frame of the first gmm of this and
            # the next bar
            offset = 0.5 * duration_beat / self.beat_subdivisions
            # first frame of first gmm that corresponds to current beat
            beat_start_frame = first_next_beat_frame
            # first frame of first gmm that corresponds to next beat
            first_next_beat_frame = int(np.floor(
                (beats[i_beat + 1, 0] - offset) * self.fps))
            # set up array with time in sec of each frame center. last frame
            # is the last frame of the last gmm of the current bar
            n_beat_frames = first_next_beat_frame - beat_start_frame
            # we need to put each feature frame in its corresponding
            # beat subdivison. We use a hack to get n_beat_frames equally
            # distributed bins between 0 and (self.beat_subdivision-1)
            beat_pos_of_frames = np.floor(
                np.linspace(0.00000001, self.beat_subdivisions - 0.00000001,
                            n_beat_frames))
            features_beat = features[beat_start_frame:first_next_beat_frame]
            # group features in list by gmms (bar position)
            feats_sorted = [features_beat[beat_pos_of_frames == x] for x in
                            np.arange(0, self.beat_subdivisions)]
            feats_sorted = self.interpolate_missing(feats_sorted, feat_dim)
            beat_features[i_beat, :, :] = np.array(
                [np.mean(x, axis=0) for x in feats_sorted])
            # beat_div[beat_pos_idx] = np.arange(1, self.beat_subdivision + 1)
        return beats, beat_features

    def interpolate_missing(self, feats_sorted, feat_dim):
        nan_fill = np.empty(feat_dim) * np.nan
        means = np.array([np.mean(x, axis=0) if len(x) > 0 else nan_fill
                          for x in feats_sorted])
        good_rows = np.unique(np.where(np.logical_not(np.isnan(means)))[0])
        if len(good_rows) < self.beat_subdivisions:
            bad_rows = np.unique(np.where(np.isnan(means))[0])
            # initialise missing values with empty array
            for r in bad_rows:
                feats_sorted[r] = np.empty((1, feat_dim))
            for d in range(feat_dim):
                means_p = np.interp(np.arange(0, self.beat_subdivisions),
                                    good_rows,
                                    means[good_rows, d])
                for r in bad_rows:
                    feats_sorted[r][0, d] = means_p[r]
        return feats_sorted


class LoadBeatsProcessor(Processor):
    """
    Load beat times from file or handle.

    """
    def __init__(self, beats, suffix=None, online=False, **kwargs):
        # FIXME: check if mode distinction is correct
        if online:
            self.mode = 'online'
        elif isinstance(beats, list) and suffix is not None:
            beats = search_files(beats, suffix=suffix)
            self.mode = 'batch'
        else:
            self.mode = 'single'
        self.beats = beats
        self.suffix = suffix

    def process(self, data=None, **kwargs):
        """
        Load the beats from file (handle) or read them from STDIN.

        """
        if self.mode == 'online':
            return self.process_online()
        elif self.mode == 'single':
            return self.process_single()
        elif self.mode == 'batch':
            return self.process_batch(data)
        else:
            raise ValueError("don't know how to obtain the beats")

    def process_online(self, *args, **kwargs):
        """
        Read the beats on a frame-by-frame basis.

        Returns
        -------
        beat : float or None
            Beat position [seconds] or None if no beat is present.

        Notes
        -----
        To be able to parse incoming beats from STDIN at the correct frame
        rate, the sender must output empty values (i.e. newlines) at the same
        rate, because this method blocks until a new value can be read in.

        """
        try:
            data = float(self.beats.readline())
        except ValueError:
            data = np.empty(0)
        return data

    def process_single(self, *args, **kwargs):
        """
        Load the beats in bulk-mode (i.e. all at once) from the input stream
        or file.

        Returns
        -------
        beats : numpy array
            Beat positions [seconds].

        """
        return load_events(self.beats)

    def process_batch(self, filename, **kwargs):
        """
        Load beat times from file.

        First match the given input filename to the beat filenames, then load
        the beats.

        Parameters
        ----------
        filename : str
            Input file name.

        Returns
        -------
        beats : numpy array
            Beat positions [seconds].

        """
        if not isinstance(filename, str):
            raise SystemExit('Please supply a filename, not %s.' % filename)
        # select the matching beat file to a given input file from all files
        basename, ext = os.path.splitext(os.path.basename(filename))
        matches = match_file(basename, self.beats, suffix=ext,
                             match_suffix=self.suffix, match_exactly=True)
        if not matches:
            raise SystemExit("can't find a beat file for %s" % filename)
        # load the beats and return them
        # TODO: Use load_beats function
        beats = np.loadtxt(matches[0])
        if beats.ndim == 2:
            # only use beat times, omit the beat positions inside the bar
            beats = beats[:, 0]
        return beats

    @classmethod
    def add_arguments(cls, parser):
        """
        Add options to save/load beats to an existing parser.

        Parameters
        ----------
        parser :
            existing argparse parser

        Returns
        -------
            beat parameter argument parser group

        """
        # add onset detection related options to the existing parser
        g = parser.add_argument_group('save/load the beat times')
        # add options for saving and loading the activations
        g.add_argument('--save_beats', action='store_true', default=False,
                       help='save the beats to file')
        g.add_argument('--load_beats', action='store_true', default=False,
                       help='load the beats from file')
        # return the argument group so it can be modified if needed
        return g


class DownbeatFeatureProcessor(SequentialProcessor):

    def __init__(self, num_bands=12, online=False, **kwargs):
        # pylint: disable=unused-argument
        from functools import partial
        from ..audio.signal import SignalProcessor, FramedSignalProcessor
        from ..audio.stft import ShortTimeFourierTransformProcessor
        from ..audio.spectrogram import (
            FilteredSpectrogramProcessor, LogarithmicSpectrogramProcessor,
            SpectrogramDifferenceProcessor)

        # percussive feature
        # FIXME: pass kwargs to the processors
        sig = SignalProcessor(**kwargs)
        # Note: we need to pass kwargs to FramedSignalProcessor, otherwise
        #       num_frames is not set correctly in online/offline mode
        frames = FramedSignalProcessor(frame_size=2048, **kwargs)
        stft = ShortTimeFourierTransformProcessor()  # caching FFT window
        filt = FilteredSpectrogramProcessor(num_bands=num_bands, fmin=60,
                                            fmax=17000, norm_filters=True)
        spec = LogarithmicSpectrogramProcessor(mul=1, add=1)
        diff = SpectrogramDifferenceProcessor(diff_ratio=0.5,
                                              positive_diffs=True)
        agg = partial(np.sum, axis=1)
        super(DownbeatFeatureProcessor, self).__init__(
            (sig, frames, stft, filt, spec, diff, agg))


class DBNBarTrackingProcessor(Processor):
    """
    Downbeat tracking with a dynamic Bayesian network (DBN).

    """

    def __init__(self, beats_per_bar=[3, 4], pattern_change_prob=0.,
                 observation_model=RNNBeatTrackingObservationModel,
                 observation_param=100, downbeats=False, online=False,
                 return_pattern=False, bump_beat_number=False, **kwargs):
        from madmom.ml.hmm import HiddenMarkovModel as Hmm
        from .beats_hmm import (BarStateSpace, BarTransitionModel,
                                MultiPatternStateSpace,
                                MultiPatternTransitionModel)
        # save variables
        self.beats_per_bar = beats_per_bar
        self.downbeats = downbeats
        # state space & transition model
        state_spaces = []
        transition_models = []
        # model each rhythmic pattern as a bar
        for beats in self.beats_per_bar:
            # Note: tempo and transition_lambda is not relevant
            st = BarStateSpace(beats, min_interval=1, max_interval=1)
            tm = BarTransitionModel(st, transition_lambda=1)
            state_spaces.append(st)
            transition_models.append(tm)
        self.st = MultiPatternStateSpace(state_spaces)
        self.tm = MultiPatternTransitionModel(
            transition_models, pattern_change_prob=pattern_change_prob)
        # observation model
        self.om = observation_model(self.st, observation_param)
        # instantiate a HMM
        self.hmm = Hmm(self.tm, self.om, None)
        # kepp state in online mode
        self.online = online
        if self.online:
            self.counter = -1  # init with -1 since we immedeatly increase it
            self.beat = np.empty((0, 2))  # [beat_time, beat_number]
            self.pattern = None
        # additional options
        self.return_pattern = return_pattern
        self.bump_beat_number = bump_beat_number

    def process(self, data, **kwargs):
        """
        Decode the beats/downbeats from the given activation function.

        Parameters
        ----------
        data : tuple (beat times, activation function)

        Returns
        -------
        numpy array
            beat or downbeat times

        """
        # get the best state path by calling the inference algorithm
        if self.online:
            return self.process_online(data, **kwargs)
        else:
            return self.process_offline(data, **kwargs)

    def process_online(self, data, **kwargs):
        # split data
        beat, activation = data
        # increase counter
        self.counter += 1
        # infer beat number and pattern only if synced beat features are
        # available
        if activation.any():
            fwd = self.hmm.forward(activation)
            # use simply the most probable state
            state = np.argmax(fwd)
            # get the position inside the bar
            position = self.st.state_positions[state]
            # corresponding beat numbers (add 1 for natural counting)
            beat_number = position.astype(int) + 1
            # save the current pattern
            pattern = self.st.state_patterns[state]
            # bump beat number if needed
            if self.bump_beat_number:
                beat_number = (beat_number % self.beats_per_bar[pattern] + 1)
            # create a beat [time, number]
            beat = np.append(beat, beat_number)
            # append pattern if needed
            if self.return_pattern:
                beat = np.append(beat, pattern)
            # and return it
            return np.array(beat, ndmin=2)
        else:
            return np.empty((0, 3))

    def process_offline(self, data, **kwargs):
        # split data
        beats, activations = data
        # Viterbi decoding
        path, _ = self.hmm.viterbi(activations)
        # get the position inside the bar
        position = self.st.state_positions[path]
        # the beat numbers are the counters + 1 at the transition points
        beat_numbers = position.astype(int) + 1
        # add the last beat
        last_beat_number = np.mod(beat_numbers[-1], self.beats_per_bar[
            self.st.state_patterns[path[-1]]]) + 1
        beat_numbers = np.append(beat_numbers, last_beat_number)
        # return the downbeats or beats and their beat numbers
        if self.downbeats:
            return np.squeeze(beats[np.where(beat_numbers == 1)])
        else:
            return np.vstack(zip(beats, beat_numbers))

    @classmethod
    def add_arguments(cls, parser, beats_per_bar=[3, 4],
                      observation_weight=100):
        """
        Add DBN related arguments to an existing parser.

        Parameters
        ----------
        parser :
            existing argparse parser
        observation_weight :
            weight for the activations at downbeat times.
        beats_per_bar :
            number of beats per bar to be modeled by the DBN

        Returns
        -------
        argparse parser

        """
        from madmom.utils import OverrideDefaultListAction
        parser = parser.add_argument_group('DBN parameters')
        parser.add_argument('--beats_per_bar',
                            action=OverrideDefaultListAction,
                            default=beats_per_bar, type=int, sep=',',
                            help='number of beats per bar to be modeled (comma'
                                 ' separated (no spaces!) list of bar length '
                                 'in beats) [default=%(default)s]')
        parser.add_argument('--observation_weight', action='store', type=int,
                            default=observation_weight,
                            help='split one beat period into N parts, the '
                                 'first representing beat states and the '
                                 'remaining non-beat states '
                                 '[default=%(default)i]')
        parser.add_argument('--pattern_change_prob', action='store',
                            type=float, default=1e-7,
                            help='pattern change probability '
                                 '[default=%(default).7f]')
        # add output format stuff
        parser = parser.add_argument_group('output arguments')
        parser.add_argument('--downbeats', action='store_true', default=False,
                            help='output only the downbeats')
        # return the argument group so it can be modified if needed
        return parser


class GMMBarProcessor(Processor):
    """
    Processor to get a downbeat activation function from multiple RNNs.

    """
    def __init__(self, pattern_files=None, pattern_change_prob=0.,
                 downbeats=False, online=False, fps=100, **kwargs):
        # load the patterns
        patterns = []
        for pattern_file in pattern_files:
            with open(pattern_file, 'rb') as f:
                # Python 2 and 3 behave differently
                # TODO: use some other format to save the GMMs (.npz, .hdf5)
                try:
                    # Python 3
                    patterns.append(pickle.load(f, encoding='latin1'))
                except TypeError:
                    # Python 2 doesn't have/need the encoding
                    patterns.append(pickle.load(f))
        if len(patterns) == 0:
            raise ValueError('at least one rhythmical pattern must be given.')
        # extract the GMMs and number of beats
        gmms = [p['gmms'] for p in patterns]
        self.num_beats = [p['num_beats'] for p in patterns]
        # self.feat_means = patterns[0]['feat_means']
        # self.feat_stds = patterns[0]['feat_stds']
        self.num_beat_divisions = [len(g[0]) for g in gmms]
        observation_model = GMMDownBeatTrackingObservationModel
        self.dbn_processor = DBNBarTrackingProcessor(
            observation_param=gmms, beats_per_bar=self.num_beats,
            observation_model=observation_model, online=online,
            pattern_change_prob=pattern_change_prob, **kwargs)

    def process(self, data, **kwargs):
        """

        Parameters
        ----------
        data : tuple (beat_time, mean_feature)

        Returns
        -------

        """
        # extract features, compute beats, and sync features to the beats
        return self.dbn_processor(data, **kwargs)

    @classmethod
    def add_arguments(cls, parser, pattern_files=None):
        """
        Add HMM related arguments to an existing parser.

        :param parser:            existing argparse parser

        Parameters for the patterns (i.e. fitted GMMs):

        :param pattern_files:     load the patterns from these files

        Parameters for the transition model:

        Each of the following arguments expect a list with as many items as
        rhythmic patterns.

        :param min_bpm:           list with minimum tempi used for tracking
        :param max_bpm:           list with maximum tempi used for tracking
        :param num_tempo_states:  list with number of tempo states (if set,
                                  limit the number of states and use a log
                                  spacing, otherwise a linear spacing)
        :param transition_lambda: list with lambdas for the exponential tempo
                                  change distribution (higher values prefer a
                                  constant tempo over a tempo change from one
                                  bar to the next one)

        Parameters for the observation model:

        :param norm_observations: normalize the observations

        :return:                  downbeat argument parser group

        """
        from madmom.utils import OverrideDefaultListAction
        # add GMM options
        if pattern_files is not None:
            g = parser.add_argument_group('GMM arguments')
            g.add_argument('--pattern_files', action=OverrideDefaultListAction,
                           default=pattern_files,
                           help='load the patterns (with the fitted GMMs) '
                                'from these files (comma separated list)')
        # add HMM parser group
        # add output format stuff
        g = parser.add_argument_group('output arguments')
        g.add_argument('--downbeats', action='store_true', default=False,
                       help='output only the downbeats')
        # return the argument group so it can be modified if needed
        g = parser.add_argument_group('Downbeat DBN arguments')
        g.add_argument('--beat_div', dest='beat_subdivisions', default=4,
                       type=int, help='Number of beat subdivisions '
                                      '[default=%(default)d]')
        return g


class GMMDownBeatTrackingObservationModel(ObservationModel):
    """
    Observation model for GMM based beat tracking with a HMM.

    """

    def __init__(self, state_space, gmms):
        """
        Construct a observation model instance using Gaussian Mixture Models
        (GMMs).

        :param gmms:              list with fitted GMM(s), one entry per
                                  rhythmic pattern
        :param state_space:  DownBeatTrackingTransitionModel instance
        :param norm_observations: normalize the observations

        "Rhythmic Pattern Modeling for Beat and Downbeat Tracking in Musical
         Audio"
        Florian Krebs, Sebastian Böck and Gerhard Widmer
        Proceedings of the 15th International Society for Music Information
        Retrieval Conference (ISMIR), 2013

        """
        self.gmms = gmms
        self.state_space = state_space
        # define the pointers of the log densities
        pointers = np.zeros(state_space.num_states, dtype=np.uint32)
        states = np.arange(self.state_space.num_states)
        pattern = self.state_space.state_patterns[states]
        position = self.state_space.state_positions[states]
        # Note: the densities of all GMMs are just stacked on top of each
        #       other, so we have to to keep track of the total number of GMMs
        densities_idx_offset = 0
        for p in range(len(gmms)):
            # distribute the observation densities defined by the GMMs
            # uniformly across the entire state space (for this pattern)
            # since the densities are just stacked, add the offset
            pointers[pattern == p] = (position[pattern == p] +
                                      densities_idx_offset)
            # number of beats for this pattern; for each beat an observation
            # likelihood is computed that encompasses all subdivision of
            # the beat
            num_beats = len(gmms[p])
            # increase the offset by the number of GMMs
            densities_idx_offset += num_beats
        # instantiate a ObservationModel with the pointers
        super(GMMDownBeatTrackingObservationModel, self).__init__(pointers)

    def log_densities(self, observations):
        """
        Computes the log densities of the observations using (a) GMM(s).

        :param observations: observations (i.e. activations of the NN)
                             [n_beats, n_subdivisions, feat_dim]
        :return:             log densities of the observations [n_beats,
                                n_states]

        """
        if observations.ndim != 3:
            raise ValueError('Got observations shape: %s, expected (n_beats, '
                             'n_subdivisions, feat_dim)' % observations.shape)
        # counter, etc.
        num_observations = observations.shape[0]
        num_patterns = len(self.gmms)
        num_states = 0
        # maximum number of GMMs of all patterns
        for i in range(num_patterns):
            num_states += len(self.gmms[i])
        # init the densities
        log_densities = np.empty((num_observations, num_states), dtype=np.float)
        # define the observation densities
        c = 0
        for i in range(num_patterns):
            for j in range(len(self.gmms[i])):  # over beat positions
                log_densities[:, c] = 0
                for bd in range(len(self.gmms[i][j])):  # over beat divisions
                    # get the predictions of each GMM for the observations
                    log_densities[:, c] += self.gmms[i][j][bd].score(
                        np.squeeze(observations[:, [bd], :], axis=1))
                c += 1
        # return the densities
        return log_densities
