"""
.. moduleauthor:: Fabio Manganiello <blacklight86@gmail.com>
"""

import json
import math
import os
import queue
import tempfile
import time

from enum import Enum
from threading import Thread, Event, RLock

from platypush.plugins import Plugin, action


class PlaybackState(Enum):
    STOPPED='STOPPED',
    PLAYING='PLAYING',
    PAUSED='PAUSED'


class RecordingState(Enum):
    STOPPED='STOPPED',
    RECORDING='RECORDING',
    PAUSED='PAUSED'


class Sound(object):
    """
    Class model a synthetic sound that can be played through the audio device
    """

    STANDARD_A_FREQUENCY = 440.0
    STANDARD_A_MIDI_NOTE = 69
    _DEFAULT_BLOCKSIZE = 2048
    _DEFAULT_BUFSIZE = 20
    _DEFAULT_SAMPLERATE = 44100

    midi_note = None
    frequency = None
    gain = 1.0
    duration = None

    def __init__(self, midi_note=midi_note, frequency=None, gain=gain,
                 duration=duration, A_frequency=STANDARD_A_FREQUENCY):
        """
        You can construct a sound either from a MIDI note or a base frequency

        :param midi_note: MIDI note code, see
            https://newt.phys.unsw.edu.au/jw/graphics/notes.GIF
        :type midi_note: int

        :param frequency: Sound base frequency in Hz
        :type frequency: float

        :param gain: Note gain/volume between 0.0 and 1.0 (default: 1.0)
        :type gain: float

        :param duration: Note duration in seconds. Default: keep until
            release/pause/stop
        :type duration: float

        :param A_frequency: Reference A4 frequency (default: 440 Hz)
        :type A_frequency: float
        """

        if midi_note and frequency:
            raise RuntimeError('Please specify either a MIDI note or a base ' +
                               'frequency')

        # TODO support for multiple notes/frequencies, either for chords or
        # harmonics

        if midi_note:
            self.midi_note = midi_note
            self.frequency = self.note_to_freq(midi_note=midi_note,
                                               A_frequency=A_frequency)
        elif frequency:
            self.frequency = frequency
            self.midi_note = self.freq_to_note(frequency=frequency,
                                               A_frequency=A_frequency)
        else:
            raise RuntimeError('Please specify either a MIDI note or a base ' +
                               'frequency')

        self.gain = gain
        self.duration = duration

    @classmethod
    def note_to_freq(cls, midi_note, A_frequency=STANDARD_A_FREQUENCY):
        """
        Converts a MIDI note to its frequency in Hz

        :param midi_note: MIDI note to convert
        :type midi_note: int

        :param A_frequency: Reference A4 frequency (default: 440 Hz)
        :type A_frequency: float
        """

        return (2.0 ** ((midi_note - cls.STANDARD_A_MIDI_NOTE) / 12.0)) \
            * A_frequency

    @classmethod
    def freq_to_note(cls, frequency, A_frequency=STANDARD_A_FREQUENCY):
        """
        Converts a frequency in Hz to its closest MIDI note

        :param frequency: Frequency in Hz
        :type midi_note: float

        :param A_frequency: Reference A4 frequency (default: 440 Hz)
        :type A_frequency: float
        """

        # TODO return also the offset in % between the provided frequency
        # and the standard MIDI note frequency
        return int(12.0 * math.log(frequency/A_frequency, 2)
                   + cls.STANDARD_A_MIDI_NOTE)

    def get_wave(self, t_start=0., t_end=0., samplerate=_DEFAULT_SAMPLERATE):
        """
        Get the wave binary data associated to this sound

        :param t_start: Start offset for the sine wave in seconds. Default: 0
        :type t_start: float

        :param t_end: End offset for the sine wave in seconds. Default: 0
        :type t_end: float

        :param samplerate: Audio sample rate. Default: 44100 Hz
        :type samplerate: int

        :returns: A numpy.ndarray[n,1] with the raw float values
        """

        import numpy as np
        x = np.linspace(t_start, t_end, int((t_end-t_start)*samplerate))

        x = x.reshape(len(x), 1)
        return self.gain * np.sin(2 * np.pi * self.frequency * x)


    def __str__(self):
        return json.dumps({
            'midi_note': midi_note,
            'frequency': frequency,
            'gain': gain,
            'duration': duration,
        })


class SoundPlugin(Plugin):
    """
    Plugin to interact with a sound device.

    Requires:

        * **sounddevice** (``pip install sounddevice``)
        * **soundfile** (``pip install soundfile``)
        * **numpy** (``pip install numpy``)
    """

    def __init__(self, input_device=None, output_device=None,
                 input_blocksize=Sound._DEFAULT_BLOCKSIZE,
                 output_blocksize=Sound._DEFAULT_BLOCKSIZE,
                 playback_bufsize=Sound._DEFAULT_BUFSIZE, *args, **kwargs):
        """
        :param input_device: Index or name of the default input device. Use :method:`platypush.plugins.sound.query_devices` to get the available devices. Default: system default
        :type input_device: int or str

        :param output_device: Index or name of the default output device. Use :method:`platypush.plugins.sound.query_devices` to get the available devices. Default: system default
        :type output_device: int or str

        :param input_blocksize: Blocksize to be applied to the input device. Try to increase this value if you get input overflow errors while recording. Default: 2048
        :type input_blocksize: int

        :param output_blocksize: Blocksize to be applied to the output device. Try to increase this value if you get output underflow errors while playing. Default: 2048
        :type output_blocksize: int

        :param playback_bufsize: Number of audio blocks that will be cached while playing (default: 20)
        :type playback_bufsize: int
        """

        super().__init__(*args, **kwargs)

        self.input_device = input_device
        self.output_device = output_device
        self.input_blocksize = input_blocksize
        self.output_blocksize = output_blocksize
        self.playback_bufsize = playback_bufsize

        self.playback_state = PlaybackState.STOPPED
        self.playback_state_lock = RLock()
        self.playback_paused_changed = Event()
        self.recording_state = RecordingState.STOPPED
        self.recording_state_lock = RLock()
        self.recording_paused_changed = Event()

    def _get_default_device(self, category):
        """
        Query the default audio devices.

        :param category: Device category to query. Can be either input or output
        :type category: str
        """

        import sounddevice as sd
        return sd.query_hostapis()[0].get('default_' + category.lower() + '_device')

    @action
    def query_devices(self, category=None):
        """
        Query the available devices

        :param category: Device category to query. Can be either input or output. Default: None (query all devices)
        :type category: str

        :returns: A dictionary representing the available devices. Example::

            [
                {
                    "name": "pulse",
                    "hostapi": 0,
                    "max_input_channels": 32,
                    "max_output_channels": 32,
                    "default_low_input_latency": 0.008684807256235827,
                    "default_low_output_latency": 0.008684807256235827,
                    "default_high_input_latency": 0.034807256235827665,
                    "default_high_output_latency": 0.034807256235827665,
                    "default_samplerate": 44100
                },
                {
                    "name": "default",
                    "hostapi": 0,
                    "max_input_channels": 32,
                    "max_output_channels": 32,
                    "default_low_input_latency": 0.008684807256235827,
                    "default_low_output_latency": 0.008684807256235827,
                    "default_high_input_latency": 0.034807256235827665,
                    "default_high_output_latency": 0.034807256235827665,
                    "default_samplerate": 44100
                }
            ]

        """

        import sounddevice as sd

        devs = sd.query_devices()
        if category == 'input':
            devs = [d for d in devs if d.get('max_input_channels') > 0]
        elif category == 'output':
            devs = [d for d in devs if d.get('max_output_channels') > 0]

        return devs


    @action
    def play(self, file=None, sounds=None, device=None, blocksize=None,
             bufsize=Sound._DEFAULT_BUFSIZE, samplerate=None, channels=None):
        """
        Plays a sound file (support formats: wav, raw) or a synthetic sound.

        :param file: Sound file path. Specify this if you want to play a file
        :type file: str

        :param sounds: Sounds to play. Specify this if you want to play
            synthetic sounds. TODO: So far only one single-frequency sound is
            supported, support for multiple sounds, chords, harmonics and mixed
            sounds is ont the way.
        :type sounds: list[Sound]. You can initialize it either from a list
            of `Sound` objects or from its JSON representation, e.g.:

                [
                    {
                        "midi_note": 69,  # 440 Hz A
                        "gain":      1.0, # Maximum volume
                        "duration":  1.0  # 1 second or until release/pause/stop
                    }
                ]

        :param device: Output device (default: default configured device or
            system default audio output if not configured)
        :type device: int or str

        :param blocksize: Audio block size (default: configured
            `output_blocksize` or 2048)
        :type blocksize: int

        :param bufsize: Size of the audio buffer (default: 20)
        :type bufsize: int

        :param samplerate: Audio samplerate. Default: audio file samplerate if
            in file mode, 44100 Hz if in synth mode
        :type samplerate: int

        :param channels: Number of audio channels. Default: number of channels
            in the audio file in file mode, 1 if in synth mode
        :type channels: int
        """

        if not file and not sounds:
            raise RuntimeError('Please specify either a file to play or a ' +
                               'list of sound objects')

        import sounddevice as sd

        if self._get_playback_state() != PlaybackState.STOPPED:
            if file:
                self.logger.info('Stopping playback before playing')
                self.stop_playback()
                time.sleep(2)

        if blocksize is None:
            blocksize = self.output_blocksize

        self.playback_paused_changed.clear()

        completed_callback_event = Event()
        q = queue.Queue(maxsize=bufsize)
        f = None
        t = 0.

        if file:
            file = os.path.abspath(os.path.expanduser(file))

        if device is None:
            device = self.output_device
        if device is None:
            device = self._get_default_device('output')

        def audio_callback(outdata, frames, time, status):
            if self._get_playback_state() == PlaybackState.STOPPED:
                raise sd.CallbackAbort

            while self._get_playback_state() == PlaybackState.PAUSED:
                self.playback_paused_changed.wait()

            assert frames == blocksize
            if status.output_underflow:
                self.logger.warning('Output underflow: increase blocksize?')
                outdata = (b'\x00' if file else 0.) * len(outdata)
                return

            assert not status

            try:
                data = q.get_nowait()
            except queue.Empty:
                self.logger.warning('Buffer is empty: increase buffersize?')
                raise sd.CallbackAbort

            if len(data) < len(outdata):
                outdata[:len(data)] = data
                outdata[len(data):] = (b'\x00' if file else 0.) * \
                    (len(outdata) - len(data))

                # if f:
                #     raise sd.CallbackStop
            else:
                outdata[:] = data


        try:
            if file:
                import soundfile as sf
                f = sf.SoundFile(file)

            if not samplerate:
                samplerate = f.samplerate if f else Sound._DEFAULT_SAMPLERATE

            if not channels:
                channels = f.channels if f else 1

            self.start_playback()
            self.logger.info('Started playback of {} to device [{}]'.
                                format(file or sounds, device))

            if sounds:
                if isinstance(sounds, str):
                    sounds = json.loads(sounds)

                for i in range(0, len(sounds)):
                    if isinstance(sounds[i], dict):
                        sounds[i] = Sound(**(sounds[i]))

            # Audio queue pre-fill loop
            for _ in range(bufsize):
                if f:
                    data = f.buffer_read(blocksize, dtype='float32')
                    if not data:
                        break
                else:
                    # TODO support for multiple sounds or mixed sounds
                    sound = sounds[0]
                    blocktime = float(blocksize / samplerate)
                    next_t = min(t+blocktime, sound.duration) \
                        if sound.duration is not None else t+blocktime

                    data = sound.get_wave(t_start=t, t_end=next_t,
                                          samplerate=samplerate)
                    t = next_t

                    if sound.duration is not None and t >= sound.duration:
                        break

                while self._get_playback_state() == PlaybackState.PAUSED:
                    self.playback_paused_changed.wait()

                if self._get_playback_state() == PlaybackState.STOPPED:
                    raise sd.CallbackAbort

                q.put_nowait(data)  # Pre-fill the audio queue

            streamtype = sd.RawOutputStream if file else sd.OutputStream
            stream = streamtype(samplerate=samplerate, blocksize=blocksize,
                                device=device, channels=channels,
                                dtype='float32', callback=audio_callback,
                                finished_callback=completed_callback_event.set)

            with stream:
                # Timeout set until we expect all the buffered blocks to
                # be consumed
                timeout = blocksize * bufsize / samplerate

                while True:
                    while self._get_playback_state() == PlaybackState.PAUSED:
                        self.playback_paused_changed.wait()

                    if f:
                        data = f.buffer_read(blocksize, dtype='float32')
                        if not data:
                            break
                    else:
                        # TODO support for multiple sounds or mixed sounds
                        sound = sounds[0]
                        blocktime = float(blocksize / samplerate)
                        next_t = min(t+blocktime, sound.duration) \
                            if sound.duration is not None else t+blocktime

                        data = sound.get_wave(t_start=t, t_end=next_t,
                                              samplerate=samplerate)
                        t = next_t

                        if sound.duration is not None and t >= sound.duration:
                            break

                    if self._get_playback_state() == PlaybackState.STOPPED:
                        raise sd.CallbackAbort

                    try:
                        q.put(data, timeout=timeout)
                    except queue.Full as e:
                        if self._get_playback_state() != PlaybackState.PAUSED:
                            raise e

                completed_callback_event.wait()
                # if sounds:
                #     sd.wait()
        except queue.Full as e:
            self.logger.warning('Playback timeout: audio callback failed?')
        finally:
            if f and not f.closed:
                f.close()
                f = None

            self.stop_playback()


    @action
    def record(self, file=None, duration=None, device=None, sample_rate=None,
               blocksize=None, latency=0, channels=1, subtype='PCM_24'):
        """
        Records audio to a sound file (support formats: wav, raw)

        :param file: Sound file (default: the method will create a temporary file with the recording)
        :type file: str

        :param duration: Recording duration in seconds (default: record until stop event)
        :type duration: float

        :param device: Input device (default: default configured device or system default audio input if not configured)
        :type device: int or str

        :param sample_rate: Recording sample rate (default: device default rate)
        :type sample_rate: int

        :param blocksize: Audio block size (default: configured `input_blocksize` or 2048)
        :type blocksize: int

        :param latency: Device latency in seconds (default: 0)
        :type latency: float

        :param channels: Number of channels (default: 1)
        :type channels: int

        :param subtype: Recording subtype - see `soundfile docs <https://pysoundfile.readthedocs.io/en/0.9.0/#soundfile.available_subtypes>`_ for a list of the available subtypes (default: PCM_24)
        :type subtype: str
        """

        import sounddevice as sd

        if self._get_recording_state() != RecordingState.STOPPED:
            self.stop_recording()
            time.sleep(2)

        self.recording_paused_changed.clear()

        if file:
            file = os.path.abspath(os.path.expanduser(file))
        else:
            file = tempfile.mktemp(prefix='platypush_recording_', suffix='.wav',
                                   dir='')

        if os.path.isfile(file):
            self.logger.info('Removing existing audio file {}'.format(file))
            os.unlink(file)

        if device is None:
            device = self.input_device
        if device is None:
            device = self._get_default_device('input')

        if sample_rate is None:
            dev_info = sd.query_devices(device, 'input')
            sample_rate = int(dev_info['default_samplerate'])

        if blocksize is None:
            blocksize = self.input_blocksize

        q = queue.Queue()

        def audio_callback(indata, frames, time, status):
            while self._get_recording_state() == RecordingState.PAUSED:
                self.recording_paused_changed.wait()

            if status:
                self.logger.warning('Recording callback status: {}'.format(
                    str(status)))

            q.put(indata.copy())


        try:
            import soundfile as sf
            import numpy

            with sf.SoundFile(file, mode='x', samplerate=sample_rate,
                              channels=channels, subtype=subtype) as f:
                with sd.InputStream(samplerate=sample_rate, device=device,
                                    channels=channels, callback=audio_callback,
                                    latency=latency, blocksize=blocksize):
                    self.start_recording()
                    self.logger.info('Started recording from device [{}] to [{}]'.
                                    format(device, file))

                    recording_started_time = time.time()

                    while self._get_recording_state() != RecordingState.STOPPED \
                            and (duration is None or
                                 time.time() - recording_started_time < duration):
                        while self._get_recording_state() == RecordingState.PAUSED:
                            self.recording_paused_changed.wait()

                        get_args = {
                            'block': True,
                            'timeout': max(0, duration - (time.time() -
                                                          recording_started_time))
                        } if duration is not None else {}

                        data = q.get(**get_args)
                        f.write(data)

                f.flush()

        except queue.Empty as e:
            self.logger.warning('Recording timeout: audio callback failed?')
        finally:
            self.stop_recording()


    @action
    def recordplay(self, duration=None, input_device=None, output_device=None,
                   sample_rate=None, blocksize=None, latency=0, channels=1,
                   dtype=None):
        """
        Records audio and plays it on an output sound device (audio pass-through)

        :param duration: Recording duration in seconds (default: record until stop event)
        :type duration: float

        :param input_device: Input device (default: default configured device or system default audio input if not configured)
        :type input_device: int or str

        :param output_device: Output device (default: default configured device or system default audio output if not configured)
        :type output_device: int or str

        :param sample_rate: Recording sample rate (default: device default rate)
        :type sample_rate: int

        :param blocksize: Audio block size (default: configured `output_blocksize` or 2048)
        :type blocksize: int

        :param latency: Device latency in seconds (default: 0)
        :type latency: float

        :param channels: Number of channels (default: 1)
        :type channels: int

        :param dtype: Data type for the recording - see `soundfile docs <https://python-sounddevice.readthedocs.io/en/0.3.12/_modules/sounddevice.html#rec>`_ for available types (default: input device default)
        :type dtype: str
        """

        import sounddevice as sd

        if self._get_playback_state() != PlaybackState.STOPPED:
            self.stop_playback()
            time.sleep(2)

        if self._get_recording_state() != RecordingState.STOPPED:
            self.stop_recording()
            time.sleep(2)

        self.playback_paused_changed.clear()
        self.recording_paused_changed.clear()

        if input_device is None:
            input_device = self.input_device
        if input_device is None:
            input_device = self._get_default_device('input')

        if output_device is None:
            output_device = self.output_device
        if output_device is None:
            output_device = self._get_default_device('output')

        if sample_rate is None:
            dev_info = sd.query_devices(input_device, 'input')
            sample_rate = int(dev_info['default_samplerate'])

        if blocksize is None:
            blocksize = self.output_blocksize

        def audio_callback(indata, outdata, frames, time, status):
            while self._get_recording_state() == RecordingState.PAUSED:
                self.recording_paused_changed.wait()

            if status:
                self.logger.warning('Recording callback status: {}'.format(
                    str(status)))

            outdata[:] = indata


        try:
            import soundfile as sf
            import numpy

            with sd.Stream(samplerate=sample_rate, channels=channels,
                           blocksize=blocksize, latency=latency,
                           device=(input_device, output_device),
                           dtype=dtype, callback=audio_callback):
                self.start_recording()
                self.start_playback()

                self.logger.info('Started recording pass-through from device ' +
                                 '[{}] to device [{}]'.
                                 format(input_device, output_device))

                recording_started_time = time.time()

                while self._get_recording_state() != RecordingState.STOPPED \
                        and (duration is None or
                             time.time() - recording_started_time < duration):
                    while self._get_recording_state() == RecordingState.PAUSED:
                        self.recording_paused_changed.wait()

                    time.sleep(0.1)

        except queue.Empty as e:
            self.logger.warning('Recording timeout: audio callback failed?')
        finally:
            self.stop_playback()
            self.stop_recording()


    def start_playback(self):
        with self.playback_state_lock:
            self.playback_state = PlaybackState.PLAYING

    @action
    def stop_playback(self):
        with self.playback_state_lock:
            self.playback_state = PlaybackState.STOPPED
        self.logger.info('Playback stopped')

    @action
    def pause_playback(self):
        with self.playback_state_lock:
            if self.playback_state == PlaybackState.PAUSED:
                self.playback_state = PlaybackState.PLAYING
            elif self.playback_state == PlaybackState.PLAYING:
                self.playback_state = PlaybackState.PAUSED
            else:
                return

        self.logger.info('Playback paused state toggled')
        self.playback_paused_changed.set()

    def start_recording(self):
        with self.recording_state_lock:
            self.recording_state = RecordingState.RECORDING

    @action
    def stop_recording(self):
        with self.recording_state_lock:
            self.recording_state = RecordingState.STOPPED
        self.logger.info('Recording stopped')

    @action
    def pause_recording(self):
        with self.recording_state_lock:
            if self.recording_state == RecordingState.PAUSED:
                self.recording_state = RecordingState.RECORDING
            elif self.recording_state == RecordingState.RECORDING:
                self.recording_state = RecordingState.PAUSED
            else:
                return

        self.logger.info('Recording paused state toggled')
        self.recording_paused_changed.set()

    def _get_playback_state(self):
        with self.playback_state_lock:
            return self.playback_state

    def _get_recording_state(self):
        with self.recording_state_lock:
            return self.recording_state

    @action
    def get_state(self):
        return {
            'playback_state': self._get_playback_state().name,
            'recording_state': self._get_recording_state().name,
        }


# vim:sw=4:ts=4:et:

