import os
from datetime import datetime
import subprocess
import warnings
import numpy as np

np.set_printoptions(suppress=True)
# andere backends sind ebenfalls möglich, brauchen aber teilweise andere dependencies
import matplotlib

matplotlib.use("Gtk3Agg")
import matplotlib.pyplot as plt

# print(plt.style.available)
plt.style.use(["seaborn-v0_8-pastel"])
import matplotlib.animation as animation
from PIL import Image
import sys
import time
import serial
import tempfile
import threading
import io
import socket
import zlib
import base64
import traceback

# Formatierung von Fehlern
try:
    from IPython.core import ultratb

    sys.excepthook = ultratb.FormattedTB(
        color_scheme="Linux", call_pdb=False  # mode="Verbose",
    )
except:
    pass

from laser_constants import *


try:
    # from stellarnet.stellarnet_driverLibs import stellarnet_driver3 as sn
    from driverLibs import stellarnet_driver3 as sn

    # print(sn.version())

    DEBUG = False
except Exception:
    print("\n Failed to load the stellarnet library.\n")
    print(traceback.format_exc())

    # exit()
    print("Running in debug-mode.\n")
    import virtual_spectrometer as sn

    DEBUG = True

spectrometer = None
wav = None
arduino = None
measurements = None
curr_measurement_index = -1

HOST = "localhost"
PORT = 8080
SOCKET = None


class PlottingSettings:
    """Wrapper, welcher die Einstellungen für einen Plot enthält."""

    file_path = ""

    # Angabe: Wellenlänge
    zoom_start = 0
    zoom_end = 0

    # nur einen Ausschnitt/Batch aus den Messungen zur Durchschnittsberechnung etc. nutzten.
    # Angabe: Index
    interval_start = 0
    interval_end = 0

    normalize_integrationtime = False
    normalize_power = False

    def __init__(
        self,
        file_path,
        zoom_start=0,
        zoom_end=sys.maxsize,
        interval_start=0,
        interval_end=sys.maxsize,
        normalize_integrationtime=False,
        normalize_power=False,
    ):
        self.file_path = file_path

        self.zoom_start = zoom_start
        self.zoom_end = zoom_end

        self.interval_start = interval_start
        self.interval_end = interval_end

        self.normalize_integrationtime = normalize_integrationtime
        self.normalize_power = normalize_power

    def sliced(self) -> bool:
        """Prüft, ob das Bild ein Ausschnitt einer Messreihe ist."""
        return not (self.interval_start == 0 and self.interval_end == sys.maxsize)

    def zoom(self) -> bool:
        """Prüft, ob das Bild ein Ausschnitt des Spektrums, welches das Spektrometer messen kann, ist."""
        return not (
            self.zoom_start == 0
            and (self.zoom_end == sys.maxsize or self.zoom_end == 2048)
        )


def arduino_setup(port, wait):
    """Verbindet sich mit dem Arduino."""
    global arduino
    arduino = serial.Serial(port=port, baudrate=9600, timeout=5)
    # auf den Arduino warten
    time.sleep(wait)
    arduino.flush()


def turn_on_laser():
    """Sendet eine Eins als Byte zum Arduino, welche ein Anschalten der Laser signalisiert."""
    arduino.write(b"1\n")
    time.sleep(ARDUINO_DELAY / 1000.0)


def turn_off_laser():
    """Sendet eine Null als Byte zum Arduino, welche ein Ausschalten der Laser signalisiert."""
    arduino.write(b"0\n")
    time.sleep(ARDUINO_DELAY / 1000.0)


# modified, von https://codingmess.blogspot.com/2009/05/conversion-of-wavelength-in-nanometers.html
def convert_wav_to_rgb(wavelength):
    """Konvertiert eine Farbe, die als Wellenlänge in Nanometern kodiert ist, zu RGB."""
    # print(np.array(wavelength).shape)
    w = int(wavelength)

    # color
    if w >= 380 and w < 440:
        r = -(w - 440.0) / (440.0 - 350.0)
        g = 0.0
        b = 1.0
    elif w >= 440 and w < 490:
        r = 0.0
        g = (w - 440.0) / (490.0 - 440.0)
        b = 1.0
    elif w >= 490 and w < 510:
        r = 0.0
        g = 1.0
        b = -(w - 510.0) / (510.0 - 490.0)
    elif w >= 510 and w < 580:
        r = (w - 510.0) / (580.0 - 510.0)
        g = 1.0
        b = 0.0
    elif w >= 580 and w < 645:
        r = 1.0
        g = -(w - 645.0) / (645.0 - 580.0)
        b = 0.0
    elif w >= 645 and w <= 780:
        r = 1.0
        g = 0.0
        b = 0.0
    else:
        r = 255.0
        g = 255.0
        b = 255.0
        return (int(r), int(g), int(b))

    # intensity correction
    if w >= 380 and w < 420:
        sss = 0.3 + 0.7 * (w - 350) / (420 - 350)
    elif w >= 420 and w <= 700:
        sss = 1.0
    elif w > 700 and w <= 780:
        sss = 0.3 + 0.7 * (780 - w) / (780 - 700)
    else:
        sss = 1 / 255.0
    sss *= 255

    return (int(sss * r), int(sss * g), int(sss * b))


def make_spectrum_image(width, height, wavelength, cache=True):
    """Erstellt das Hintergrundbild eines Plots (den Regenbogen)."""

    if cache:
        try:
            return Image.open(
                "{0}/spectrum_background_{1}_{2}.png".format(
                    tempfile.gettempdir(), width, height
                )
            )
        except:
            pass

    image = Image.new("RGB", (width, height), (255, 255, 255))

    for x in range(width):
        for y in range(height):
            image.putpixel((x, y), convert_wav_to_rgb(wavelength[x]))  #  + (200,)

    if cache:
        image.save(
            "{0}/spectrum_background_{1}_{2}.png".format(
                tempfile.gettempdir(), width, height
            )
        )

    return image


def spectrometer_setup():
    """Verbindet sich mit dem Spektrometer."""

    global spectrometer, wav

    spectrometer, wav = sn.array_get_spec(0)

    wav = wav.reshape(
        2048,
    )

    # es gibt 2048 Elemente: Jeder der 864 Wellenlängen ist immer mit zwei bis drei Nachkommastellen vertreten
    # print(wav[0], wav[-1]) -> 285.24 1149.4808101739814
    # for i in wav:
    #     print(i)
    # print(len(wav))

    # print(spectrometer)
    # print("\nDevice ID: ", sn.getDeviceId(spectrometer))

    sn.ext_trig(spectrometer, True)
    # True ignoriert die ersten Messdaten, da diese ungenau sein können (durch Änderung der Integrationszeit)
    sn.setParam(spectrometer, INTTIME, SCAN_AVG, SMOOTH, XTIMING, True)


def get_data():
    """Liest die Daten des Spektrometers aus."""
    # start_time = time.time()
    # var = sn.array_spectrum(spectrometer, wav)
    # print((time.time() - start_time) * 1000)
    # return var
    # print(sn.array_spectrum(spectrometer, wav).shape)  # (2048, 2)
    # print(sn.getSpectrum_Y(spectrometer).shape)  # (2048,)

    # return sn.array_spectrum(spectrometer, wav)
    return sn.getSpectrum_Y(spectrometer)


def plot_results(
    plotting_settings: list[PlottingSettings],
    verbose=True,
):
    """Erstellt den Plot anhand der gemessenen Daten."""

    # [:]: nutze eine Kopie von plotting_settings zum Iterieren
    for setting in plotting_settings[:]:
        file_list = [
            os.path.join(setting.file_path, f)
            for f in os.listdir(setting.file_path)
            if f.endswith(".npz")
        ]
        if not file_list:
            print("the dir {0} ist empty!".format(setting.file_path))
            plotting_settings.remove(setting)

    iterator = 0

    fig, ax = plt.subplots()
    plt.grid(True)

    for setting in plotting_settings:

        file_list = [
            os.path.join(setting.file_path, f)
            for f in os.listdir(setting.file_path)
            if f.endswith(".npz")
        ]

        # colors = plt.cm.jet(np.linspace(0, 1, len(file_list) + 2))
        colors = matplotlib.colormaps["jet"](np.linspace(0, 1, len(file_list) + 2))

        # temp_data = np.load(file_list[0])["arr_0"][0]
        # # print(temp_data.shape)
        # # exit()
        # temp_waves = []
        # for val in temp_data:
        #     temp_waves.append(val[0])

        # temp_waves = np.array(temp_waves)

        wavelengths = np.load(file_list[0])["arr_1"]

        if setting.zoom_end == sys.maxsize:
            setting.zoom_end = 2048
        else:
            setting.zoom_end = (np.abs(wavelengths - setting.zoom_end)).argmin()

        if setting.zoom_start != 0:
            setting.zoom_start = (np.abs(wavelengths - setting.zoom_start)).argmin()

        x_ax_len = setting.zoom_end - setting.zoom_start

        result = np.zeros([x_ax_len], dtype=float)
        standard_deviation_data = []
        if verbose:
            print("calculating average...")

        for file in file_list:

            if verbose:
                print("reading data from disk...")
            loaded_array = np.load(file)
            spectrometer_data = loaded_array["arr_0"]
            metadata = loaded_array["arr_2"]

            assert metadata[REPETITIONS_INDEX] == len(spectrometer_data)

            normalize_integrationtime_factor = 1
            normalize_power = 1

            if setting.normalize_integrationtime:
                normalize_integrationtime_factor = metadata[INTTIME_INDEX]
            if setting.normalize_power:
                normalize_integrationtime_factor = metadata[INTENSITY_INDEX]

            mean = np.zeros([x_ax_len], dtype=float)

            for i in range(len(spectrometer_data)):
                j = i + setting.interval_start
                if j >= setting.interval_end:
                    break
                intensity = []
                for k in range(len(spectrometer_data[j])):
                    l = k + setting.zoom_start
                    if l >= setting.zoom_end:
                        break
                    intensity.append(
                        spectrometer_data[j][l]
                        / normalize_integrationtime_factor
                        / normalize_power
                    )
                standard_deviation_data.append(intensity)

                mean += spectrometer_data[j][setting.zoom_start : setting.zoom_end]

            print(
                setting.interval_end - setting.interval_start
                if setting.sliced()
                else len(spectrometer_data)
            )
            print(setting.sliced())
            mean /= (
                setting.interval_end - setting.interval_start
                if setting.sliced()
                else len(spectrometer_data)
            )
            result += mean

        result /= len(file_list)
        standard_deviation = np.std(standard_deviation_data, axis=0)

        # wave_min = min(wavelengths)
        # wave_max = max(wavelengths)
        # assert wave_min == wavelengths[0]
        # assert wave_max == wavelengths[-1]
        wave_min = wavelengths[0]
        wave_max = wavelengths[-1]

        intensity_min = min(result)
        intensity_max = max(result)
        std_max = max(standard_deviation)

        if verbose:
            print("creating backgroundimage...")
        # wird resized, deshalb height=1
        ax.imshow(
            make_spectrum_image(int(len(wavelengths)), 1, wavelengths),
            extent=[
                wave_min,
                wave_max,
                intensity_min - std_max,
                intensity_max + std_max,
            ],
            aspect="auto",
            alpha=0.4,
        )

        if verbose:
            print("creating plot...")

        rate = metadata[REPETITIONS_INDEX]
        if setting.sliced():
            rate = setting.interval_end - setting.interval_start

        ax.scatter(
            wavelengths,
            result,
            label="Mittelwert von {0} Messungen".format(rate),
            color=colors[iterator],
            s=1,
        )
        assert len(result) == len(standard_deviation)

        ax.fill_between(
            wavelengths,
            result - standard_deviation,
            result + standard_deviation,
            alpha=0.5,
            edgecolor="#CC4F1B",
            facecolor="#FF9848",
            label="Standardabweichung",
        )
        iterator += 1

    fig.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.05),
        fancybox=True,
        shadow=True,
        markerscale=4,
    )  # ,ncols=3

    plt.xlabel("Wellenlänge in nm")
    plt.ylabel("Intensität in Counts")

    title = NAME

    for setting in plotting_settings:
        if setting.sliced():
            title = "{0} bis {1} Laserschüsse {2}".format(
                setting.interval_start, setting.interval_end, NAME
            )
        if setting.normalize_integrationtime:
            title += " Integrationszeit normalisiert"
        if setting.normalize_power:
            title += " Power normalisiert"

        if setting.zoom():
            title += " Ausschnitt"
        # nur vom ersten hinzufügen, ansonsten wird der Titel zu lang
        break

    plt.title(title)
    title += ".jpg"

    plt.draw()

    try:
        os.makedirs("Plots/", 0o777)
    except OSError as error:
        print(error)
        print("(directory is already existent)")

    fig.savefig("Plots/" + title.replace("%", "Prozent"), dpi=300, bbox_inches="tight")
    if verbose:
        print("saved plot")

    plt.show()
    plt.close()
    plt.cla()
    plt.clf()


def plot_to_image(fig):
    """Konvertiert einen Matplotlib Plot zu einem PIL Image."""
    buf = io.BytesIO()
    fig.savefig(buf, dpi=300, bbox_inches="tight", format="jpg")  # Alpha entfernen
    buf.seek(0)
    # Image.open(buf).save("buffered.png")
    return Image.open(buf)


def image_to_array(image):
    """Konvertiert ein PIL Image zu einem Array."""

    width, height = image.size
    bytes_list = bytearray()

    for x in range(width):
        for y in range(height):
            coordinate = x, y
            pixel = image.getpixel(coordinate)
            bytes_list.append(pixel[0])
            bytes_list.append(pixel[1])
            bytes_list.append(pixel[2])
            # if pixel[0] is not 255:
            #     print(str(image.getpixel(coordinate)))

    return bytes_list


def send_plot():
    """ "Sendet den aktuellen Plot an das spezifizierte Socket."""
    global SOCKET

    while True:
        if SOCKET is None:
            try:
                SOCKET = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                SOCKET.connect((HOST, PORT))
            except:
                SOCKET = None
                continue

        live_plotting()

        img = plot_to_image(live_fig)
        img.thumbnail((600, 500))

        SOCKET.sendto(
            ("size {0} {1}\n".format(img.size[0], img.size[1])).encode("utf-8"),
            (HOST, PORT),
        )
        message = image_to_array(img)
        SOCKET.sendto(
            base64.b64encode(zlib.compress(message)) + b"\n", (HOST, PORT)
        )  # .encode('utf-8')

        # data = sock.recv(1024)


live_fig, live_ax = plt.subplots()
plt.xlabel("Wellenlänge in nm")
plt.ylabel("Intensität in Counts")
plt.grid(True)
plt.title(NAME)

scatter = live_ax.scatter([], [], label="Mittelwert von {0} Messungen".format(0), s=5)

past_measurement_index = -1


def live_plotting(i=None):
    """Plottet die aktuell gemessene Messung."""

    global past_measurement_index

    if (
        len(measurements) == 0
        or curr_measurement_index < 0
        or past_measurement_index == curr_measurement_index
    ):
        return

    measurement = measurements[curr_measurement_index]

    # die Daten im Scatter-Plot aktualisieren
    scatter.set_offsets(np.column_stack((wav, measurement)))

    scatter.set_label("Spektrum von Messung {0}".format(curr_measurement_index + 1))

    # den Wertebereich der Axen anpassen
    live_ax.set_xlim(wav[0], wav[-1])
    # live_ax.set_ylim(min(measurement), max(measurement))
    live_ax.set_ylim(0, 3000)

    live_ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.05),
        fancybox=True,
        shadow=True,
        markerscale=4,
    )
    past_measurement_index = curr_measurement_index
    return (scatter,)


plt.ion()
plt.show()

ani = animation.FuncAnimation(live_fig, live_plotting, interval=10, frames=REPETITIONS)


def time_measurement(iter: int):

    seconds_list = [time.time()]

    for _ in range(iter):
        turn_on_laser()
        time.sleep(IRRADITION_TIME / 1000.0)
        get_data()
        turn_off_laser()
        time.sleep(MEASUREMENT_DELAY / 1000.0)
        # print(i)
        seconds_list.append(time.time())

    total_time_millis = int(round(time.time() * 1000)) - int(
        round(seconds_list[0] * 1000)
    )
    print("measurements took: {0} ms".format(total_time_millis))
    delays_time = (MEASUREMENT_DELAY + 2 * ARDUINO_DELAY + IRRADITION_TIME) * iter
    print("thereof delays: {0} ms".format(delays_time))
    print("a measurement took: {0} ms".format(total_time_millis / 1.0 / iter))
    print("std: +/- " + str(np.std(np.array(seconds_list) - seconds_list[0])))
    print("without delays:")
    print(
        "a measurement took: {0} ms".format(
            (total_time_millis - delays_time) / 1.0 / iter
        )
    )
    print(
        "std: +/- "
        + str(
            np.std(
                np.array(seconds_list)
                - seconds_list[0]
                - (MEASUREMENT_DELAY + 2 * ARDUINO_DELAY + IRRADITION_TIME)
            )
        )
    )


def suppressed_pause():
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        # UserWarning: Starting a Matplotlib GUI outside of the main thread will likely fail.
        plt.pause(0.1)


def start_gui_thread():

    threading.Thread(
        target=lambda: [suppressed_pause() for _ in iter(int, 1)],
        daemon=True,  # Main-Thread soll nicht auf den Thread warten
    ).start()

    # threading.Thread(target=send_plot, daemon=True).start()
    # plt.ioff()

    # warten, bis der Thread gestartet ist
    time.sleep(0.5)


if __name__ == "__main__":

    spectrometer_setup()
    measurements = np.zeros([100, 2048], dtype=float)
    start_gui_thread()

    try:
        while True:
            next_measurement_index = (curr_measurement_index + 1) % len(measurements)
            measurements[next_measurement_index] = get_data()
            time.sleep(MEASUREMENT_DELAY / 1000.0)
            curr_measurement_index = next_measurement_index
    except KeyboardInterrupt:
        sn.reset(spectrometer)
        exit()

    """
    ## alle Messungen plotten
    for messungs_dir in os.listdir("Messungen"):
        NAME = messungs_dir
        print(NAME)
        plot_results([PlottingSettings("Messungen/" + messungs_dir)])

    NAME = "Langzeitmessung mit Superkontinuumlaser mit Blaulichtlasern mit 100 %"
    #  zoom_start=642, zoom_end=662
    plot_results([PlottingSettings("Messungen/" + NAME, zoom_start=600, zoom_end=700)])

    NAME = "Langzeitmessung mit Superkontinuumlaser und Blaulichtlasern, 100.000 Schuss, Messreihe 3 mit 100 %"
    #  zoom_start=642, zoom_end=662
    plot_results([PlottingSettings("Messungen/" + NAME, zoom_start=600, zoom_end=700)])

    AUSSCHNITT = True

    ## eine Messung gestückelt plotten
    slice_sice = 10_000
    NAME = "Langzeitmessung mit Superkontinuumlaser mit Blaulichtlasern mit 100 %"
    for i in range(0, 100_000, slice_sice):
        print("range from {0} - {1} ".format(i, i + slice_sice))
        plot_results(
            [
                PlottingSettings(
                    "Messungen/" + NAME,
                    zoom_start=600,
                    zoom_end=700,
                    intervall_start=i,
                    intervall_end=i + slice_sice - 1,
                )
            ]
        )

    ## eine Messung gestückelt plotten
    slice_sice = 10_000
    NAME = "Langzeitmessung mit Superkontinuumlaser und Blaulichtlasern, 100.000 Schuss, Messreihe 3 mit 100 %"
    for i in range(0, 100_000, slice_sice):
        print("range from {0} - {1} ".format(i, i + slice_sice))
        plot_results(
            [
                PlottingSettings(
                    "Messungen/" + NAME,
                    zoom_start=600,
                    zoom_end=700,
                    intervall_start=i,
                    intervall_end=i + slice_sice - 1,
                )
            ]
        )
    AUSSCHNITT = False
    ## eine Messung gestückelt plotten
    slice_sice = 10_000
    NAME = "Langzeitmessung mit Superkontinuumlaser mit Blaulichtlasern mit 100 %"
    for i in range(0, 100_000, slice_sice):
        print("range from {0} - {1} ".format(i, i + slice_sice))
        plot_results(
            [
                PlottingSettings(
                    "Messungen/" + NAME,
                    intervall_start=i,
                    intervall_end=i + slice_sice - 1,
                )
            ]
        )

    ## eine Messung gestückelt plotten
    slice_sice = 10_000
    NAME = "Langzeitmessung mit Superkontinuumlaser und Blaulichtlasern, 100.000 Schuss, Messreihe 3 mit 100 %"
    for i in range(0, 100_000, slice_sice):
        print("range from {0} - {1} ".format(i, i + slice_sice))
        plot_results(
            [
                PlottingSettings(
                    "Messungen/" + NAME,
                    intervall_start=i,
                    intervall_end=i + slice_sice - 1,
                )
            ]
        )
    exit()
    """

    if DEBUG:
        NAME += "_DEBUG"
        DIR_PATH = (
            os.path.dirname(os.path.realpath(__file__))
            + "/Messungen/Debug/"
            + NAME
            + "/"
        )
    else:
        DIR_PATH = (
            os.path.dirname(os.path.realpath(__file__)) + "/Messungen/" + NAME + "/"
        )

    try:
        os.makedirs(DIR_PATH, 0o777)
    except OSError as error:
        print(error)
        print("(directory is already existent)")

    spectrometer_setup()
    ## bitte entfernen.
    if not CONTINOUS:
        arduino_setup("/dev/ttyUSB0", 1)

    """
    measurements = []

    for i in range (REPETITIONS):
        measurements.append(get_data())
        time.sleep(DELAY / 1000.0)
    """
    # minimal schneller als jedes Mal list.append()
    measurements = np.zeros([REPETITIONS, 2048], dtype=float)

    start_gui_thread()

    print("\nrepetitions:")

    seconds = time.time()

    if CONTINOUS:
        for i in range(REPETITIONS):
            measurements[i] = get_data()
            time.sleep(MEASUREMENT_DELAY / 1000.0)
            sys.stdout.write("\r")
            sys.stdout.write(" " + str(i))
            sys.stdout.flush()
            curr_measurement_index = i
            # print(i)
    else:
        for i in range(REPETITIONS):
            turn_on_laser()
            time.sleep(IRRADITION_TIME / 1000.0)
            measurements[i] = get_data()
            turn_off_laser()
            time.sleep(MEASUREMENT_DELAY / 1000.0)
            sys.stdout.write("\r")
            sys.stdout.write(" " + str(i))
            sys.stdout.flush()
            curr_measurement_index = i
            # print(i)
            if time.time() - seconds > TIMEOUT:
                print("\nreached timeout!")
                break
    # \r resetten
    print()
    total_time_millis = int(round(time.time() * 1000)) - int(round(seconds * 1000))
    print("measurements took: {0} ms".format(total_time_millis))
    delays_time = (
        MEASUREMENT_DELAY + 2 * ARDUINO_DELAY + IRRADITION_TIME
    ) * REPETITIONS
    print("thereof delays: {0} ms".format(delays_time))
    print("a measurement took: {0} ms".format(total_time_millis / 1.0 / REPETITIONS))
    print("without delays:")
    print(
        "a measurement took: {0} ms".format(
            (total_time_millis - delays_time) / 1.0 / REPETITIONS
        )
    )

    # Spektrometer freigeben
    sn.reset(spectrometer)

    metadata = np.zeros(9, dtype=int)
    metadata[INTTIME_INDEX] = INTTIME
    metadata[INTENSITY_INDEX] = INTENSITY
    metadata[SCAN_AVG_INDEX] = SCAN_AVG
    metadata[SMOOTH_INDEX] = SMOOTH
    metadata[XTIMING_INDEX] = XTIMING
    metadata[REPETITIONS_INDEX] = REPETITIONS
    metadata[ARDUINO_DELAY_INDEX] = ARDUINO_DELAY
    metadata[IRRADITION_TIME_INDEX] = IRRADITION_TIME
    metadata[CONTINOUS_INDEX] = int(CONTINOUS)

    if OVERWRITE:
        file_name = DIR_PATH + "Messung"
    else:
        # manche Dateisysteme unterstützen keinen Doppelpunkt im Dateinamen
        file_name = DIR_PATH + str(datetime.now()).replace(":", "_")

    np.savez_compressed(
        file_name, np.array(measurements), np.array(wav), np.array(metadata)
    )
    os.chmod(file_name + ".npz", 0o777)

    plot_results([PlottingSettings(DIR_PATH)])
