from enum import Enum
import numpy as np
from scipy.signal import welch
import pyqtgraph as pg
from PyQt5 import QtCore

from acconeer_utils.clients import SocketClient, SPIClient, UARTClient
from acconeer_utils.clients import configs
from acconeer_utils import example_utils
from acconeer_utils.pg_process import PGProcess, PGProccessDiedException
from acconeer_utils.structs import configbase


HALF_WAVELENGTH = 2.445e-3  # m
NUM_FFT_BINS = 512
HISTORY_LENGTH = 2.0  # s
EST_VEL_HISTORY_LENGTH = HISTORY_LENGTH  # s
SD_HISTORY_LENGTH = HISTORY_LENGTH  # s
NUM_SAVED_SEQUENCES = 10
SEQUENCE_TIMEOUT_COUNT = 10


def main():
    args = example_utils.ExampleArgumentParser(num_sens=1).parse_args()
    example_utils.config_logging(args)

    if args.socket_addr:
        client = SocketClient(args.socket_addr)
    elif args.spi:
        client = SPIClient()
    else:
        port = args.serial_port or example_utils.autodetect_serial_port()
        client = UARTClient(port)

    sensor_config = get_sensor_config()
    processing_config = get_processing_config()
    sensor_config.sensor = args.sensors

    session_info = client.setup_session(sensor_config)

    pg_updater = PGUpdater(sensor_config, processing_config, session_info)
    pg_process = PGProcess(pg_updater)
    pg_process.start()

    client.start_streaming()

    interrupt_handler = example_utils.ExampleInterruptHandler()
    print("Press Ctrl-C to end session")

    processor = Processor(sensor_config, processing_config, session_info)

    while not interrupt_handler.got_signal:
        info, sweep = client.get_next()
        plot_data = processor.process(sweep)

        if plot_data is not None:
            try:
                pg_process.put_data(plot_data)
            except PGProccessDiedException:
                break

    print("Disconnecting...")
    pg_process.close()
    client.disconnect()


def get_sensor_config():
    config = configs.SparseServiceConfig()

    config.range_interval = [0.30, 0.48]
    config.stepsize = 3
    config.sampling_mode = configs.SparseServiceConfig.SAMPLING_MODE_A
    config.number_of_subsweeps = NUM_FFT_BINS
    config.gain = 0.5
    config.hw_accelerated_average_samples = 60
    # config.subsweep_rate = 6e3

    # force max frequency
    config.sweep_rate = 200
    config.experimental_stitching = True

    return config


class ProcessingConfiguration(configbase.ProcessingConfig):
    VERSION = 1

    class SpeedUnit(Enum):
        METER_PER_SECOND = ("m/s", 1)
        KILOMETERS_PER_HOUR = ("km/h", 3.6)
        MILES_PER_HOUR = ("mph", 2.237)

        @property
        def label(self):
            return self.value[0]

        @property
        def scale(self):
            return self.value[1]

    min_speed = configbase.FloatParameter(
            label="Minimum speed",
            unit="m/s",
            default_value=0.1,
            limits=(0, 10),
            updateable=True,
            order=0,
            )

    shown_speed_unit = configbase.EnumParameter(
            label="Speed unit",
            default_value=SpeedUnit.METER_PER_SECOND,
            enum=SpeedUnit,
            updateable=True,
            order=100,
            )

    show_data_plot = configbase.BoolParameter(
            label="Show data",
            default_value=False,
            updateable=True,
            order=110,
            )

    show_sd_plot = configbase.BoolParameter(
            label="Show spectral density",
            default_value=True,
            updateable=True,
            order=120,
            )

    show_vel_history_plot = configbase.BoolParameter(
            label="Show speed history",
            default_value=True,
            updateable=True,
            order=130,
            )


get_processing_config = ProcessingConfiguration


class Processor:
    def __init__(self, sensor_config, processing_config, session_info):
        subsweep_rate = session_info["actual_subsweep_rate"]
        est_update_rate = subsweep_rate / sensor_config.number_of_subsweeps

        self.nperseg = NUM_FFT_BINS // 2
        self.num_noise_est_bins = 3
        noise_est_tc = 1.0
        self.min_threshold = 4.0
        self.dynamic_threshold = 0.1

        est_vel_history_size = int(round(est_update_rate * EST_VEL_HISTORY_LENGTH))
        sd_history_size = int(round(est_update_rate * SD_HISTORY_LENGTH))
        num_bins = NUM_FFT_BINS // 2 + 1
        self.noise_est_sf = self.tc_to_sf(noise_est_tc, est_update_rate)
        self.bin_fs = np.fft.rfftfreq(NUM_FFT_BINS) * subsweep_rate
        self.bin_vs = self.bin_fs * HALF_WAVELENGTH

        self.nasd_history = np.zeros([sd_history_size, num_bins])
        self.est_vel_history = np.full(est_vel_history_size, np.nan)
        self.belongs_to_last_sequence = np.zeros(est_vel_history_size, dtype=bool)
        self.noise_est = 0
        self.current_sequence_idle = SEQUENCE_TIMEOUT_COUNT + 1
        self.sequence_vels = np.zeros(NUM_SAVED_SEQUENCES)
        self.update_idx = 0

        self.update_processing_config(processing_config)

    def update_processing_config(self, processing_config):
        self.min_speed = processing_config.min_speed

    def tc_to_sf(self, tc, fs):
        if tc <= 0.0:
            return 0.0

        return np.exp(-1.0 / (tc * fs))

    def dynamic_sf(self, static_sf):
        return min(static_sf, 1.0 - 1.0 / (1.0 + self.update_idx))

    def process(self, sweep):
        # Basic speed estimate

        zero_mean_sweep = sweep - sweep.mean(axis=0, keepdims=True)

        _, psds = welch(
                zero_mean_sweep,
                nperseg=self.nperseg,
                detrend=False,
                axis=0,
                nfft=NUM_FFT_BINS,
                )

        psd = np.max(psds, axis=1)
        asd = np.sqrt(psd)

        inst_noise_est = np.mean(asd[-self.num_noise_est_bins:])
        sf = self.dynamic_sf(self.noise_est_sf)
        self.noise_est = sf * self.noise_est + (1.0 - sf) * inst_noise_est

        nasd = asd / self.noise_est

        threshold = max(self.min_threshold, np.max(nasd) * self.dynamic_threshold)
        over = nasd > threshold
        est_idx = np.where(over)[0][-1] if np.any(over) else np.nan

        if est_idx > 0:  # evaluates to false if nan
            est_vel = self.bin_vs[est_idx]
        else:
            est_vel = np.nan

        if est_vel < self.min_speed:  # evaluates to false if nan
            est_vel = np.nan

        # Sequence

        self.belongs_to_last_sequence = np.roll(self.belongs_to_last_sequence, -1)

        if np.isnan(est_vel):
            self.current_sequence_idle += 1
        else:
            if self.current_sequence_idle > SEQUENCE_TIMEOUT_COUNT:
                self.sequence_vels = np.roll(self.sequence_vels, -1)
                self.sequence_vels[-1] = est_vel
                self.belongs_to_last_sequence[:] = False

            self.current_sequence_idle = 0
            self.belongs_to_last_sequence[-1] = True

            if est_vel > self.sequence_vels[-1]:
                self.sequence_vels[-1] = est_vel

        # Data for plots

        self.est_vel_history = np.roll(self.est_vel_history, -1, axis=0)
        self.est_vel_history[-1] = est_vel

        if np.all(np.isnan(self.est_vel_history)):
            output_vel = None
        else:
            output_vel = np.nanmax(self.est_vel_history)

        self.nasd_history = np.roll(self.nasd_history, -1, axis=0)
        self.nasd_history[-1] = nasd

        nasd_temporal_max = np.max(self.nasd_history, axis=0)

        temporal_max_threshold = max(
            self.min_threshold, np.max(nasd_temporal_max) * self.dynamic_threshold)

        self.update_idx += 1

        return {
            "sweep": sweep,
            "sd": nasd_temporal_max,
            "sd_threshold": temporal_max_threshold,
            "vel_history": self.est_vel_history,
            "vel": output_vel,
            "sequence_vels": self.sequence_vels,
            "belongs_to_last_sequence": self.belongs_to_last_sequence,
        }


class PGUpdater:
    def __init__(self, sensor_config, processing_config, session_info):
        self.processing_config = processing_config

        self.num_subsweeps = sensor_config.number_of_subsweeps
        self.subsweep_rate = session_info["actual_subsweep_rate"]
        self.depths = get_range_depths(sensor_config, session_info)
        self.num_depths = self.depths.size
        self.est_update_rate = self.subsweep_rate / self.num_subsweeps

        self.bin_vs = np.fft.rfftfreq(NUM_FFT_BINS) * self.subsweep_rate * HALF_WAVELENGTH
        self.dt = 1.0 / self.est_update_rate

        self.setup_is_done = False

    def setup(self, win):
        # Data plots

        self.data_plots = []
        self.data_curves = []
        for i in range(self.num_depths):
            title = "{:.0f} cm".format(100 * self.depths[i])
            plot = win.addPlot(row=0, col=i, title=title)
            plot.showGrid(x=True, y=True)
            plot.setYRange(-2**15, 2**15)
            plot.hideAxis("left")
            plot.hideAxis("bottom")
            plot.plot(np.arange(self.num_subsweeps), np.zeros(self.num_subsweeps))
            curve = plot.plot(pen=example_utils.pg_pen_cycler())
            self.data_plots.append(plot)
            self.data_curves.append(curve)

        # Spectral density plot

        self.sd_plot = win.addPlot(row=1, col=0, colspan=self.num_depths)
        self.sd_plot.setLabel("left", "Normalized ASD")
        self.sd_plot.showGrid(x=True, y=True)
        self.sd_curve = self.sd_plot.plot(pen=example_utils.pg_pen_cycler())
        dashed_pen = pg.mkPen("k", width=2, style=QtCore.Qt.DashLine)
        self.sd_threshold_line = pg.InfiniteLine(angle=0, pen=dashed_pen)
        self.sd_plot.addItem(self.sd_threshold_line)

        self.smooth_max = example_utils.SmoothMax(
                self.est_update_rate,
                tau_decay=0.5,
                tau_grow=0,
                hysteresis=0.2,
                )

        # Rolling speed plot

        self.vel_plot = pg.PlotItem()
        self.vel_plot.setLabel("bottom", "Time (s)")
        self.vel_plot.showGrid(x=True, y=True)
        self.vel_plot.setXRange(-EST_VEL_HISTORY_LENGTH, 0)
        self.vel_max_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("k", width=1))
        self.vel_plot.addItem(self.vel_max_line)
        self.vel_scatter = pg.ScatterPlotItem(size=8)
        self.vel_plot.addItem(self.vel_scatter)

        self.vel_html_fmt = '<span style="color:#000;font-size:24pt;">{:.1f} {}</span>'
        self.vel_text_item = pg.TextItem(anchor=(0.5, 0))
        self.vel_plot.addItem(self.vel_text_item)

        # Sequence speed plot

        self.sequences_plot = pg.PlotItem()
        self.sequences_plot.setLabel("bottom", "History")
        self.sequences_plot.showGrid(y=True)
        self.sequences_plot.setXRange(-NUM_SAVED_SEQUENCES + 0.5, 0.5)
        tmp = np.flip(np.arange(NUM_SAVED_SEQUENCES) == 0)
        brushes = [pg.mkBrush(example_utils.color_cycler(n)) for n in tmp]
        self.bar_graph = pg.BarGraphItem(
                x=np.arange(-NUM_SAVED_SEQUENCES, 0) + 1,
                height=np.zeros(NUM_SAVED_SEQUENCES),
                width=0.8,
                brushes=brushes,
                )
        self.sequences_plot.addItem(self.bar_graph)

        self.sequences_text_item = pg.TextItem(anchor=(0.5, 0))
        self.sequences_plot.addItem(self.sequences_text_item)

        sublayout = win.addLayout(row=2, col=0, colspan=self.num_depths)
        sublayout.addItem(self.vel_plot, col=0)
        sublayout.addItem(self.sequences_plot, col=1)

        self.setup_is_done = True
        self.update_processing_config()

    def update_processing_config(self, processing_config=None):
        if processing_config is None:
            processing_config = self.processing_config
        else:
            self.processing_config = processing_config

        if not self.setup_is_done:
            return

        for plot in self.data_plots:
            plot.setVisible(processing_config.show_data_plot)

        self.sd_plot.setVisible(processing_config.show_sd_plot)
        self.vel_plot.setVisible(processing_config.show_vel_history_plot)

        self.unit = processing_config.shown_speed_unit
        speed_label = "Speed ({})".format(self.unit.label)
        self.sd_plot.setLabel("bottom", speed_label)
        self.vel_plot.setLabel("left", speed_label)
        self.sequences_plot.setLabel("left", speed_label)
        max_vel = self.bin_vs[-1] * self.unit.scale
        self.sd_plot.setXRange(0, max_vel)

        y_max = max_vel * 1.2
        self.vel_plot.setYRange(0, y_max)
        self.sequences_plot.setYRange(0, y_max)
        self.vel_text_item.setPos(-EST_VEL_HISTORY_LENGTH / 2, y_max)
        self.sequences_text_item.setPos(-NUM_SAVED_SEQUENCES / 2 + 0.5, y_max)

    def update(self, data):
        # Data plots

        for i, ys in enumerate(data["sweep"].T):
            self.data_curves[i].setData(ys)

        # Spectral density plot

        sd = data["sd"]
        m = self.smooth_max.update(max(10, np.max(sd)))
        self.sd_plot.setYRange(0, m)
        self.sd_curve.setData(self.bin_vs * self.unit.scale, sd)

        self.sd_threshold_line.setPos(data["sd_threshold"])

        # Rolling speed plot

        vs = data["vel_history"] * self.unit.scale
        mask = ~np.isnan(vs)
        ts = -np.flip(np.arange(vs.size)) * self.dt
        bs = data["belongs_to_last_sequence"]
        brushes = [example_utils.pg_brush_cycler(int(b)) for b in bs[mask]]

        self.vel_scatter.setData(ts[mask], vs[mask], brush=brushes)

        v = data["vel"]
        if v:
            html = self.vel_html_fmt.format(v * self.unit.scale, self.unit.label)
            self.vel_text_item.setHtml(html)
            self.vel_text_item.show()

            self.vel_max_line.setPos(v)
            self.vel_max_line.show()
        else:
            self.vel_text_item.hide()
            self.vel_max_line.hide()

        # Sequence speed plot

        hs = data["sequence_vels"] * self.unit.scale
        self.bar_graph.setOpts(height=hs)

        if hs[-1] > 1e-3:
            html = self.vel_html_fmt.format(hs[-1], self.unit.label)
            self.sequences_text_item.setHtml(html)


def get_range_depths(sensor_config, session_info):
    range_start = session_info["actual_range_start"]
    range_end = range_start + session_info["actual_range_length"]
    num_depths = session_info["data_length"] // sensor_config.number_of_subsweeps
    return np.linspace(range_start, range_end, num_depths)


if __name__ == "__main__":
    main()