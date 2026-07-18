"""
Tektronix DMM4050 Windows GUI
=============================

Purpose
-------
A Windows desktop GUI for a Tektronix DMM4050 connected through the
Xyphro UsbGpib adapter at GPIB address 10.

Release R05
-----------
- Restored the proven full-timeout USBTMC read method used by R03.
- Corrected empty USBTMC response handling with controlled retry logic.
- Removed 500 ms segmented VISA reads that were incompatible with the
  Xyphro UsbGpib V2.3 and R&S VISA combination used on the target PC.
- Added explicit transport-error classification and safer recovery logging.
- Ensured VISA close occurs only after the acquisition worker has exited.
- Retains bounded graph history, CSV synchronization, adaptive window sizing,
  and automatic engineering-unit graph scaling.

Main features
-------------
- Automatic detection of USB0::0x03EB::0x2065::GPIB_10_...::INSTR
- All primary DMM4050 measurement functions
- User-selectable sample interval
- Large live-value display
- Rolling or static graph
- Automatic engineering-unit graph scaling such as µV, mA, kΩ, and MHz
- Plain-language Y-axis labels without scientific-notation multipliers
- Optional low and high warning limits
- Optional CSV recording
- Start, stop, single-reading, clear-graph, connect, and disconnect controls
- Scrollable interface with vertical and horizontal scroll bars
- Automatic attempt to return the meter to LOCAL mode on disconnect
- Threaded VISA communication so the GUI remains responsive

Required packages
-----------------
    py -m pip install pyvisa matplotlib

Run
---
    py dmm4050_gui_gpib10_R05.py
"""

from __future__ import annotations

import csv
import math
import os
import sys
import queue
import re
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import pyvisa
    from pyvisa import constants
    from pyvisa.errors import VisaIOError
except ImportError as exc:
    raise SystemExit(
        "PyVISA is required. Install it with:\n"
        "py -m pip install pyvisa"
    ) from exc

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter
except ImportError as exc:
    raise SystemExit(
        "Matplotlib is required for the live graph. Install it with:\n"
        "py -m pip install matplotlib"
    ) from exc


# ===========================================================================
# Connection and application constants
# ===========================================================================

APP_TITLE: Final[str] = "Tektronix DMM4050 Measurement Console"
APP_VERSION: Final[str] = "R05"

USB_GPIB_VENDOR_ID: Final[str] = "0X03EB"
USB_GPIB_PRODUCT_ID: Final[str] = "0X2065"
TARGET_GPIB_ADDRESS: Final[int] = 10

OPEN_TIMEOUT_MS: Final[int] = 10_000
IO_TIMEOUT_MS: Final[int] = 20_000
EMPTY_RESPONSE_MAX_READS: Final[int] = 4
EMPTY_RESPONSE_RETRY_DELAY_S: Final[float] = 0.050
VISA_TIMEOUT_ERROR_CODE: Final[int] = -1073807339
GRAPH_HISTORY_MAX_POINTS: Final[int] = 100_000
SHUTDOWN_DELAY_WARNING_MS: Final[int] = 15_000
OVERRANGE_LIMIT: Final[float] = 9.0e36

# Requested minimalist high-contrast palette.
COLOR_BG: Final[str] = "#0B1220"
COLOR_PANEL: Final[str] = "#111827"
COLOR_PANEL_ALT: Final[str] = "#182233"
COLOR_BORDER: Final[str] = "#334155"
COLOR_TEXT: Final[str] = "#F8FAFC"
COLOR_MUTED: Final[str] = "#A7B0BF"
COLOR_BLUE: Final[str] = "#4F7CAC"
COLOR_LIVE: Final[str] = "#4C956C"
COLOR_WARNING: Final[str] = "#ECC94B"
COLOR_DANGER: Final[str] = "#E53E3E"
COLOR_SUCCESS: Final[str] = "#38A169"
COLOR_DISABLED: Final[str] = "#475569"
COLOR_ENTRY: Final[str] = "#0F172A"
COLOR_GRAPH_LINE: Final[str] = "#78A6D1"

FONT_FAMILY: Final[str] = "Segoe UI"


# ===========================================================================
# Measurement definitions
# ===========================================================================

@dataclass(frozen=True)
class MeasurementDefinition:
    key: str
    group: str
    name: str
    command: str
    unit: str
    connection_note: str
    special_setup: str = ""


@dataclass(frozen=True)
class MeasurementPlan:
    key: str
    name: str
    command: str
    unit: str
    connection_note: str


@dataclass(frozen=True)
class GraphScale:
    """Engineering scale applied only to the on-screen graph."""

    multiplier: float
    display_unit: str
    exponent: int


MEASUREMENTS: Final[tuple[MeasurementDefinition, ...]] = (
    MeasurementDefinition(
        "dc_voltage",
        "Voltage",
        "DC Voltage",
        "MEASure:VOLTage:DC?",
        "V",
        "Connect the signal to INPUT HI and INPUT LO.",
    ),
    MeasurementDefinition(
        "ac_voltage",
        "Voltage",
        "AC Voltage",
        "MEASure:VOLTage:AC?",
        "V",
        "Connect the signal to INPUT HI and INPUT LO.",
    ),
    MeasurementDefinition(
        "dc_ratio",
        "Voltage",
        "DC Voltage Ratio",
        "MEASure:VOLTage:DC:RATio?",
        "V/V",
        (
            "Connect the measured voltage to INPUT HI/LO and the reference "
            "voltage to SENSE HI/LO. Connect the two LO terminals together."
        ),
    ),
    MeasurementDefinition(
        "dc_current",
        "Current",
        "DC Current",
        "MEASure:CURRent:DC?",
        "A",
        (
            "Use the correct current terminal and connect the DMM in series. "
            "Confirm the expected current is within the selected terminal rating."
        ),
    ),
    MeasurementDefinition(
        "ac_current",
        "Current",
        "AC Current",
        "MEASure:CURRent:AC?",
        "A",
        (
            "Use the correct current terminal and connect the DMM in series. "
            "Confirm the expected current is within the selected terminal rating."
        ),
    ),
    MeasurementDefinition(
        "resistance_2w",
        "Resistance and Test",
        "2-Wire Resistance",
        "MEASure:RESistance?",
        "Ω",
        "Remove external power. Connect the device to INPUT HI and INPUT LO.",
    ),
    MeasurementDefinition(
        "resistance_4w",
        "Resistance and Test",
        "4-Wire Resistance",
        "MEASure:FRESistance?",
        "Ω",
        "Use INPUT HI/LO for source and SENSE HI/LO for Kelvin sensing.",
    ),
    MeasurementDefinition(
        "continuity",
        "Resistance and Test",
        "Continuity",
        "MEASure:CONTinuity?",
        "Ω",
        "Remove external power. Connect the circuit to INPUT HI and INPUT LO.",
    ),
    MeasurementDefinition(
        "diode",
        "Resistance and Test",
        "Diode Test",
        "MEASure:DIODe?",
        "V",
        "Remove external power. Connect the diode to the voltage input terminals.",
        "diode",
    ),
    MeasurementDefinition(
        "capacitance",
        "Frequency and Other",
        "Capacitance",
        "MEASure:CAPacitance?",
        "F",
        "Fully discharge the capacitor before connection. Observe polarity.",
    ),
    MeasurementDefinition(
        "frequency",
        "Frequency and Other",
        "Frequency",
        "MEASure:FREQuency?",
        "Hz",
        "Connect the signal to INPUT HI and INPUT LO.",
    ),
    MeasurementDefinition(
        "period",
        "Frequency and Other",
        "Period",
        "MEASure:PERiod?",
        "s",
        "Connect the signal to INPUT HI and INPUT LO.",
    ),
    MeasurementDefinition(
        "temperature_2w",
        "Temperature",
        "Temperature, 2-Wire RTD",
        "MEASure:TEMPerature:RTD?",
        "°C",
        "Connect the RTD using the DMM4050 2-wire RTD arrangement.",
        "rtd2",
    ),
    MeasurementDefinition(
        "temperature_4w",
        "Temperature",
        "Temperature, 4-Wire RTD",
        "MEASure:TEMPerature:FRTD?",
        "°C",
        "Connect the RTD using INPUT HI/LO and SENSE HI/LO.",
        "rtd4",
    ),
)

MEASUREMENT_BY_KEY: Final[dict[str, MeasurementDefinition]] = {
    item.key: item for item in MEASUREMENTS
}


# ===========================================================================
# Graph engineering-unit helpers
# ===========================================================================

ENGINEERING_PREFIXES: Final[dict[int, str]] = {
    -12: "p",
    -9: "n",
    -6: "µ",
    -3: "m",
    0: "",
    3: "k",
    6: "M",
    9: "G",
}

SCALABLE_GRAPH_UNITS: Final[set[str]] = {
    "V",
    "A",
    "Ω",
    "F",
    "Hz",
    "s",
}


def select_graph_scale(
    values: list[float] | tuple[float, ...],
    base_unit: str,
) -> GraphScale:
    """
    Select a readable engineering unit for graph display.

    Raw measurements and CSV values remain in the DMM base SI unit.
    Only the plotted values are scaled.
    """
    if base_unit not in SCALABLE_GRAPH_UNITS:
        return GraphScale(
            multiplier=1.0,
            display_unit=base_unit,
            exponent=0,
        )

    finite_values = [
        abs(value)
        for value in values
        if math.isfinite(value)
        and not is_overrange(value)
    ]

    if not finite_values:
        return GraphScale(
            multiplier=1.0,
            display_unit=base_unit,
            exponent=0,
        )

    maximum = max(finite_values)

    if maximum <= 0.0:
        exponent = 0
    else:
        exponent = int(
            math.floor(math.log10(maximum) / 3.0) * 3
        )
        exponent = max(-12, min(9, exponent))

    prefix = ENGINEERING_PREFIXES[exponent]

    return GraphScale(
        multiplier=10.0 ** (-exponent),
        display_unit=f"{prefix}{base_unit}",
        exponent=exponent,
    )


def format_plain_axis_tick(value: float, _position: float) -> str:
    """
    Format graph ticks without scientific notation.

    The plotted data are already converted to an engineering unit, so the
    tick labels should remain direct values such as 1.87, 250, or 0.025.
    """
    if not math.isfinite(value):
        return ""

    if abs(value) < 1.0e-14:
        value = 0.0

    absolute = abs(value)

    if absolute >= 1000.0:
        result = f"{value:,.0f}"
    elif absolute >= 100.0:
        result = f"{value:.1f}"
    elif absolute >= 10.0:
        result = f"{value:.2f}"
    elif absolute >= 1.0:
        result = f"{value:.3f}"
    elif absolute >= 0.1:
        result = f"{value:.4f}"
    elif absolute >= 0.01:
        result = f"{value:.5f}"
    else:
        result = f"{value:.6f}"

    if "." in result:
        result = result.rstrip("0").rstrip(".")

    return result


# ===========================================================================
# DMM4050 VISA interface
# ===========================================================================


class MeasurementCancelled(RuntimeError):
    """Raised when the operator cancels an active DMM query."""


class DMMCommunicationError(RuntimeError):
    """Raised when the VISA transport does not return a valid DMM response."""


def is_visa_timeout(error: BaseException) -> bool:
    """Return True when a PyVISA exception represents VI_ERROR_TMO."""
    error_code = getattr(error, "error_code", None)

    try:
        if error_code is not None and int(error_code) == VISA_TIMEOUT_ERROR_CODE:
            return True
    except (TypeError, ValueError):
        pass

    text_value = str(error).upper()
    return "VI_ERROR_TMO" in text_value or "TIMEOUT" in text_value


class DMM4050:
    """Thread-safe VISA interface for the DMM4050 and Xyphro UsbGpib."""

    def __init__(self) -> None:
        self.resource_manager: pyvisa.ResourceManager | None = None
        self.instrument = None
        self.resource_name = ""
        self.identity = ""
        self.lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return self.instrument is not None

    def connect(self) -> dict[str, str]:
        """Locate, open, and verify the DMM4050 at GPIB address 10."""
        with self.lock:
            if self.connected:
                return self.read_status()

            self.resource_manager = pyvisa.ResourceManager()
            self.resource_name = self._find_resource(self.resource_manager)

            self.instrument = self.resource_manager.open_resource(
                self.resource_name,
                open_timeout=OPEN_TIMEOUT_MS,
            )

            # These settings match the communication method already proven
            # on the user's DMM4050 and Xyphro UsbGpib installation.
            self.instrument.timeout = IO_TIMEOUT_MS
            self.instrument.send_end = True
            self.instrument.write_termination = None
            self.instrument.read_termination = None

            self.write("*CLS")
            time.sleep(0.20)
            self.identity = self.query("*IDN?")

            if "DMM4050" not in self.identity.upper():
                response = self.identity
                self.close(go_local=False)
                raise RuntimeError(
                    "The connected instrument did not identify as a "
                    f"Tektronix DMM4050. Response: {response}"
                )

            return self.read_status()

    @staticmethod
    def _find_resource(
        resource_manager: pyvisa.ResourceManager,
    ) -> str:
        resources = resource_manager.list_resources("?*")
        address_pattern = re.compile(
            rf"GPIB_{TARGET_GPIB_ADDRESS}(?:_|::)",
            re.IGNORECASE,
        )

        matches = [
            resource
            for resource in resources
            if USB_GPIB_VENDOR_ID in resource.upper()
            and USB_GPIB_PRODUCT_ID in resource.upper()
            and address_pattern.search(resource)
        ]

        if not matches:
            available = "\n".join(f"  {item}" for item in resources)
            if not available:
                available = "  No VISA resources were found."

            raise RuntimeError(
                "No Xyphro UsbGpib resource was found at GPIB address 10.\n\n"
                "Available VISA resources:\n"
                f"{available}\n\n"
                "Confirm the DMM4050 interface is IEEE488, the GPIB address "
                "is 10, and the command language is SCPI."
            )

        return matches[0]

    def _require_instrument_unlocked(self):
        if self.instrument is None:
            raise RuntimeError("The DMM4050 is not connected.")
        return self.instrument

    def _write_unlocked(self, command: str) -> None:
        instrument = self._require_instrument_unlocked()
        payload = command.encode("ascii") + b"\n"
        instrument.write_raw(payload)

    def _clear_unlocked(self) -> None:
        """Best-effort device clear used after a cancelled read."""
        instrument = self._require_instrument_unlocked()

        try:
            instrument.clear()
        except Exception:
            # Some USBTMC bridges do not implement device clear. The caller
            # still exits safely and may reconnect before the next session.
            pass

    def write(self, command: str) -> None:
        """Send one LF-terminated SCPI command."""
        with self.lock:
            self._write_unlocked(command)

    def _query_unlocked(
        self,
        command: str,
        cancel_event: threading.Event | None = None,
        timeout_ms: int = IO_TIMEOUT_MS,
    ) -> str:
        """
        Send one SCPI query and return one non-empty response.

        The Xyphro UsbGpib V2.3 adapter with R&S VISA was verified using a
        normal blocking read. Short segmented reads can return an empty USBTMC
        message before the DMM4050 result is ready. This implementation keeps
        the proven full-timeout read behavior and treats empty messages as
        transient transport frames rather than valid instrument responses.
        """
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be greater than zero.")

        instrument = self._require_instrument_unlocked()
        original_timeout = instrument.timeout
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        empty_read_count = 0

        try:
            if cancel_event is not None and cancel_event.is_set():
                raise MeasurementCancelled(
                    "The measurement was cancelled before the query started."
                )

            self._write_unlocked(command)

            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._clear_unlocked()
                    raise MeasurementCancelled(
                        "The active measurement was cancelled by the operator."
                    )

                remaining_ms = int(
                    max(0.0, deadline - time.monotonic()) * 1000.0
                )

                if remaining_ms <= 0:
                    raise DMMCommunicationError(
                        f"Timed out waiting for a response to {command!r} "
                        f"after {timeout_ms} ms."
                    )

                # Use the complete remaining timeout for each read. This is
                # intentionally not split into 500 ms polling operations.
                instrument.timeout = max(1, remaining_ms)

                try:
                    response_bytes = instrument.read_raw()
                except VisaIOError as exc:
                    if is_visa_timeout(exc):
                        raise DMMCommunicationError(
                            f"Timed out waiting for a response to {command!r} "
                            f"after {timeout_ms} ms."
                        ) from exc

                    raise DMMCommunicationError(
                        f"VISA read failed for {command!r}: {exc}"
                    ) from exc

                if cancel_event is not None and cancel_event.is_set():
                    raise MeasurementCancelled(
                        "The active measurement was cancelled by the operator."
                    )

                response = response_bytes.decode(
                    "ascii",
                    errors="replace",
                ).strip()

                if response:
                    return response

                empty_read_count += 1

                if empty_read_count >= EMPTY_RESPONSE_MAX_READS:
                    raise DMMCommunicationError(
                        f"The DMM4050 returned {empty_read_count} empty USBTMC "
                        f"responses to {command!r}. Reconnect the UsbGpib "
                        "adapter before retrying."
                    )

                # Prevent a tight loop when a VISA backend returns an empty
                # transfer immediately instead of waiting for instrument data.
                time.sleep(EMPTY_RESPONSE_RETRY_DELAY_S)

        finally:
            try:
                instrument.timeout = original_timeout
            except Exception:
                pass

    def query(self, command: str) -> str:
        """Send one SCPI query using the validated DMM4050 transport method."""
        with self.lock:
            return self._query_unlocked(command)

    def query_cancellable(
        self,
        command: str,
        cancel_event: threading.Event,
        overall_timeout_ms: int = IO_TIMEOUT_MS,
    ) -> str:
        """
        Execute a measurement query with safe operator-cancellation checks.

        A VISA read already in progress cannot be interrupted reliably through
        this USBTMC bridge. Stop is therefore checked immediately before and
        after the blocking read. The GUI remains responsive, and shutdown waits
        for the acquisition thread before closing the VISA session.
        """
        with self.lock:
            return self._query_unlocked(
                command=command,
                cancel_event=cancel_event,
                timeout_ms=overall_timeout_ms,
            )

    def query_float(self, command: str) -> float:
        response = self.query(command)
        return self._parse_float_response(command, response)

    def query_float_cancellable(
        self,
        command: str,
        cancel_event: threading.Event,
    ) -> float:
        response = self.query_cancellable(command, cancel_event)
        return self._parse_float_response(command, response)

    @staticmethod
    def _parse_float_response(command: str, response: str) -> float:
        if not response:
            raise DMMCommunicationError(
                f"No response was received for {command!r}."
            )

        try:
            return float(response)
        except ValueError as exc:
            raise DMMCommunicationError(
                f"Unexpected response to {command!r}: {response!r}"
            ) from exc

    def read_status(self) -> dict[str, str]:
        """Read useful settings without failing the complete connection."""
        status: dict[str, str] = {
            "Identity": self.identity,
            "Resource": self.resource_name,
            "GPIB Address": str(TARGET_GPIB_ADDRESS),
        }

        optional_queries = {
            "Active Function": "FUNCtion?",
            "Input Terminals": "ROUTe:TERMinals?",
            "SCPI Version": "SYSTem:VERSion?",
            "Error Queue": "SYSTem:ERRor?",
        }

        for label, command in optional_queries.items():
            try:
                status[label] = self.query(command)
            except Exception:
                status[label] = "Unavailable"

        return status

    def check_error(self) -> str:
        try:
            return self.query("SYSTem:ERRor?")
        except Exception as exc:
            return f"Unable to read DMM error queue: {exc}"

    def return_to_local(self) -> tuple[bool, str]:
        """
        Attempt IEEE-488 Go To Local.

        Some USBTMC bridges do not expose control_ren. The method returns a
        clear result so the GUI can instruct the operator when the LOCAL soft
        key must be pressed on the meter.
        """
        with self.lock:
            if self.instrument is None:
                return True, "The VISA session is already closed."

            try:
                self.instrument.control_ren(
                    constants.RENLineOperation.deassert_gtl
                )
                time.sleep(0.20)
                return True, "Go To Local command sent."
            except Exception as exc:
                return (
                    False,
                    "Automatic LOCAL control is not supported by this "
                    f"adapter or VISA provider: {exc}",
                )

    def close(self, go_local: bool = True) -> tuple[bool, str]:
        """Close the instrument and VISA resource manager."""
        local_ok = True
        local_message = "LOCAL mode was not requested."

        with self.lock:
            if self.instrument is not None:
                if go_local:
                    local_ok, local_message = self.return_to_local()

                try:
                    self.instrument.close()
                finally:
                    self.instrument = None

            if self.resource_manager is not None:
                try:
                    self.resource_manager.close()
                finally:
                    self.resource_manager = None

            self.identity = ""
            self.resource_name = ""

        return local_ok, local_message


# ===========================================================================
# Utility functions
# ===========================================================================

def is_overrange(value: float) -> bool:
    return not math.isfinite(value) or abs(value) >= OVERRANGE_LIMIT


def engineering_text(value: float, unit: str) -> str:
    """Format a reading using engineering prefixes."""
    if is_overrange(value):
        return "OVERLOAD / OPEN"

    if unit == "°C":
        return f"{value:.6f} °C"

    if unit == "V/V":
        return f"{value:.9g} V/V"

    if value == 0:
        return f"0 {unit}"

    prefixes = {
        -12: "p",
        -9: "n",
        -6: "µ",
        -3: "m",
        0: "",
        3: "k",
        6: "M",
        9: "G",
    }

    exponent = int(math.floor(math.log10(abs(value)) / 3.0) * 3)
    exponent = max(-12, min(9, exponent))
    scaled = value / (10.0 ** exponent)

    return f"{scaled:.9g} {prefixes[exponent]}{unit}"


def safe_float(text: str) -> float | None:
    stripped = text.strip()
    if not stripped:
        return None

    try:
        value = float(stripped)
    except ValueError as exc:
        raise ValueError(f"{text!r} is not a valid number.") from exc

    if not math.isfinite(value):
        raise ValueError("The value must be finite.")

    return value


# ===========================================================================
# Scrollable container
# ===========================================================================

class ScrollableFrame(tk.Frame):
    """Frame with vertical and horizontal scroll bars."""

    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, bg=COLOR_BG, **kwargs)

        self.canvas = tk.Canvas(
            self,
            bg=COLOR_BG,
            highlightthickness=0,
            borderwidth=0,
        )
        self.v_scroll = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
        )
        self.h_scroll = ttk.Scrollbar(
            self,
            orient="horizontal",
            command=self.canvas.xview,
        )

        self.canvas.configure(
            yscrollcommand=self.v_scroll.set,
            xscrollcommand=self.h_scroll.set,
        )

        self.content = tk.Frame(self.canvas, bg=COLOR_BG)
        self.window_id = self.canvas.create_window(
            (0, 0),
            window=self.content,
            anchor="nw",
        )

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.v_scroll.grid(row=0, column=1, sticky="ns")
        self.h_scroll.grid(row=1, column=0, sticky="ew")

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.content.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._resize_content)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _update_scroll_region(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_content(self, event: tk.Event) -> None:
        required = self.content.winfo_reqwidth()
        width = max(event.width, required)
        self.canvas.itemconfigure(self.window_id, width=width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if event.state & 0x0001:
            self.canvas.xview_scroll(int(-event.delta / 120), "units")
        else:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


# ===========================================================================
# GUI application
# ===========================================================================

class DMM4050App(tk.Tk):
    """Windows GUI for DMM4050 measurement, display, graphing, and CSV."""

    def __init__(self) -> None:
        super().__init__()

        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.configure(bg=COLOR_BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()

        usable_width = max(720, screen_width - 40)
        usable_height = max(520, screen_height - 80)
        width = min(1500, int(screen_width * 0.92), usable_width)
        height = min(980, int(screen_height * 0.88), usable_height)
        width = max(720, width)
        height = max(520, height)

        x_pos = max(0, (screen_width - width) // 2)
        y_pos = max(0, (screen_height - height) // 3)
        self.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

        minimum_width = min(900, max(640, screen_width - 120))
        minimum_height = min(620, max(480, screen_height - 160))
        self.minsize(minimum_width, minimum_height)

        self.option_add("*Font", (FONT_FAMILY, 10))
        self.option_add("*TCombobox*Listbox.background", COLOR_ENTRY)
        self.option_add("*TCombobox*Listbox.foreground", COLOR_TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", COLOR_BLUE)
        self.option_add("*TCombobox*Listbox.selectForeground", COLOR_TEXT)

        self._configure_styles()

        self.dmm = DMM4050()
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.connect_thread: threading.Thread | None = None
        self.running = False
        self.closing = False

        # These collections are owned exclusively by the Tk main thread.
        # A bounded deque prevents an unattended graph session from consuming
        # unbounded memory. CSV recording remains complete and independent.
        self.elapsed_data: deque[float] = deque(
            maxlen=GRAPH_HISTORY_MAX_POINTS
        )
        self.value_data: deque[float] = deque(
            maxlen=GRAPH_HISTORY_MAX_POINTS
        )
        self.last_sample_times: deque[float] = deque(maxlen=20)
        self.graph_dirty = False
        self.session_start_monotonic = 0.0
        self.sample_count = 0

        self.selected_measurement = tk.StringVar(value="dc_voltage")
        self.sample_interval_var = tk.StringVar(value="1.0")
        self.graph_mode_var = tk.StringVar(value="Rolling")
        self.rolling_points_var = tk.StringVar(value="300")

        self.record_csv_var = tk.BooleanVar(value=False)
        self.csv_path_var = tk.StringVar(value="")

        self.low_limit_var = tk.StringVar(value="")
        self.high_limit_var = tk.StringVar(value="")

        self.diode_mode_var = tk.StringVar(value="Default")
        self.rtd_type_var = tk.StringVar(value="PT100_385")
        self.rtd_r0_var = tk.StringVar(value="100.0")
        self.rtd_alpha_var = tk.StringVar(value="0.00385")

        self.connection_state_var = tk.StringVar(value="DISCONNECTED")
        self.run_state_var = tk.StringVar(value="IDLE")
        self.live_value_var = tk.StringVar(value="No reading")
        self.raw_value_var = tk.StringVar(value="Raw value: --")
        self.timestamp_var = tk.StringVar(value="Timestamp: --")
        self.rate_var = tk.StringVar(value="Actual rate: --")
        self.count_var = tk.StringVar(value="Samples: 0")
        self.function_var = tk.StringVar(value="Function: DC Voltage")
        self.connection_note_var = tk.StringVar(
            value=MEASUREMENT_BY_KEY["dc_voltage"].connection_note
        )
        self.resource_var = tk.StringVar(value="Resource: not connected")
        self.identity_var = tk.StringVar(value="Instrument: not connected")
        self.terminal_var = tk.StringVar(value="Input terminals: unknown")
        self.csv_status_var = tk.StringVar(value="CSV recording disabled")
        self.graph_scale_var = tk.StringVar(
            value="Y-axis: waiting for measurement data"
        )
        self.advanced_title_var = tk.StringVar(value="Function Configuration")
        self.dmm_error_var = tk.StringVar(value="DMM error queue: not checked")

        self.configuration_widgets: list[tk.Widget] = []
        self.measurement_buttons: list[ttk.Radiobutton] = []

        self._build_header()
        self._build_scrollable_body()
        self._build_footer()

        self.after(100, self._process_event_queue)
        self.after(250, self._refresh_graph_if_needed)
        self.after(700, self.connect_async)

    # ------------------------------------------------------------------
    # Style and layout helpers
    # ------------------------------------------------------------------

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(
            ".",
            background=COLOR_PANEL,
            foreground=COLOR_TEXT,
            fieldbackground=COLOR_ENTRY,
            bordercolor=COLOR_BORDER,
            lightcolor=COLOR_BORDER,
            darkcolor=COLOR_BORDER,
            font=(FONT_FAMILY, 10),
        )

        style.configure(
            "TScrollbar",
            background=COLOR_PANEL_ALT,
            troughcolor=COLOR_BG,
            bordercolor=COLOR_BG,
            arrowcolor=COLOR_TEXT,
        )

        style.configure(
            "TCombobox",
            fieldbackground=COLOR_ENTRY,
            background=COLOR_PANEL_ALT,
            foreground=COLOR_TEXT,
            arrowcolor=COLOR_TEXT,
            bordercolor=COLOR_BORDER,
            padding=7,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", COLOR_ENTRY)],
            foreground=[("readonly", COLOR_TEXT)],
            selectbackground=[("readonly", COLOR_ENTRY)],
            selectforeground=[("readonly", COLOR_TEXT)],
        )

        style.configure(
            "Dark.TEntry",
            fieldbackground=COLOR_ENTRY,
            foreground=COLOR_TEXT,
            insertcolor=COLOR_TEXT,
            bordercolor=COLOR_BORDER,
            padding=7,
        )

        style.configure(
            "Function.TRadiobutton",
            background=COLOR_PANEL_ALT,
            foreground=COLOR_TEXT,
            padding=(10, 7),
            indicatorcolor=COLOR_ENTRY,
        )
        style.map(
            "Function.TRadiobutton",
            background=[
                ("active", COLOR_BORDER),
                ("selected", COLOR_BLUE),
                ("disabled", COLOR_PANEL),
            ],
            foreground=[
                ("disabled", COLOR_MUTED),
                ("selected", COLOR_TEXT),
            ],
            indicatorcolor=[
                ("selected", COLOR_SUCCESS),
                ("disabled", COLOR_DISABLED),
            ],
        )

        style.configure(
            "Dark.TCheckbutton",
            background=COLOR_PANEL,
            foreground=COLOR_TEXT,
            padding=5,
        )
        style.map(
            "Dark.TCheckbutton",
            background=[("active", COLOR_PANEL)],
            foreground=[("disabled", COLOR_MUTED)],
            indicatorcolor=[
                ("selected", COLOR_SUCCESS),
                ("disabled", COLOR_DISABLED),
            ],
        )

    def _panel(
        self,
        parent: tk.Misc,
        title: str,
        row: int,
        column: int,
        columnspan: int = 1,
        sticky: str = "nsew",
        padx: tuple[int, int] = (8, 8),
        pady: tuple[int, int] = (8, 8),
    ) -> tuple[tk.Frame, tk.Frame]:
        outer = tk.Frame(
            parent,
            bg=COLOR_PANEL,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
        )
        outer.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky=sticky,
            padx=padx,
            pady=pady,
        )

        title_label = tk.Label(
            outer,
            text=title,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            font=(FONT_FAMILY, 12, "bold"),
            anchor="w",
            padx=14,
            pady=10,
        )
        title_label.pack(fill="x")

        separator = tk.Frame(outer, bg=COLOR_BORDER, height=1)
        separator.pack(fill="x")

        body = tk.Frame(outer, bg=COLOR_PANEL)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        return outer, body

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command,
        background: str,
        width: int = 14,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg=COLOR_TEXT,
            activebackground=background,
            activeforeground=COLOR_TEXT,
            disabledforeground="#CBD5E1",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            font=(FONT_FAMILY, 10, "bold"),
            padx=12,
            pady=9,
            width=width,
            cursor="hand2",
        )

    def _build_header(self) -> None:
        header = tk.Frame(
            self,
            bg=COLOR_PANEL,
            highlightbackground=COLOR_BORDER,
            highlightthickness=0,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        title_block = tk.Frame(header, bg=COLOR_PANEL)
        title_block.grid(row=0, column=0, sticky="w", padx=18, pady=12)

        tk.Label(
            title_block,
            text=APP_TITLE,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            font=(FONT_FAMILY, 17, "bold"),
        ).pack(anchor="w")

        tk.Label(
            title_block,
            text=(
                f"Xyphro UsbGpib | GPIB address {TARGET_GPIB_ADDRESS} | "
                f"Version {APP_VERSION}"
            ),
            bg=COLOR_PANEL,
            fg=COLOR_MUTED,
            font=(FONT_FAMILY, 9),
        ).pack(anchor="w", pady=(2, 0))

        status_block = tk.Frame(header, bg=COLOR_PANEL)
        status_block.grid(row=0, column=2, sticky="e", padx=18, pady=12)

        self.connection_badge = tk.Label(
            status_block,
            textvariable=self.connection_state_var,
            bg=COLOR_DISABLED,
            fg=COLOR_TEXT,
            font=(FONT_FAMILY, 9, "bold"),
            padx=12,
            pady=6,
        )
        self.connection_badge.pack(side="left", padx=(0, 8))

        self.run_badge = tk.Label(
            status_block,
            textvariable=self.run_state_var,
            bg=COLOR_BLUE,
            fg=COLOR_TEXT,
            font=(FONT_FAMILY, 9, "bold"),
            padx=12,
            pady=6,
        )
        self.run_badge.pack(side="left")

    def _build_scrollable_body(self) -> None:
        self.scroll_frame = ScrollableFrame(self)
        self.scroll_frame.grid(row=1, column=0, sticky="nsew")
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        content = self.scroll_frame.content
        content.grid_columnconfigure(0, weight=1, minsize=540)
        content.grid_columnconfigure(1, weight=1, minsize=540)

        self._build_connection_panel(content)
        self._build_measurement_panel(content)
        self._build_acquisition_panel(content)
        self._build_advanced_panel(content)
        self._build_live_panel(content)
        self._build_graph_panel(content)
        self._build_log_panel(content)

    def _build_footer(self) -> None:
        footer = tk.Frame(
            self,
            bg=COLOR_PANEL,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
        )
        footer.grid(row=2, column=0, sticky="ew")

        self.footer_status = tk.Label(
            footer,
            text="Ready. The application will attempt to connect automatically.",
            bg=COLOR_PANEL,
            fg=COLOR_MUTED,
            anchor="w",
            padx=14,
            pady=7,
            font=(FONT_FAMILY, 9),
        )
        self.footer_status.pack(fill="x")

    # ------------------------------------------------------------------
    # Panel construction
    # ------------------------------------------------------------------

    def _build_connection_panel(self, parent: tk.Misc) -> None:
        _, body = self._panel(
            parent,
            "1. Instrument Connection",
            row=0,
            column=0,
            columnspan=2,
        )
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)

        information = tk.Frame(body, bg=COLOR_PANEL)
        information.grid(row=0, column=0, sticky="ew")
        information.grid_columnconfigure(0, weight=1)

        for row, variable in enumerate(
            (
                self.identity_var,
                self.resource_var,
                self.terminal_var,
                self.dmm_error_var,
            )
        ):
            tk.Label(
                information,
                textvariable=variable,
                bg=COLOR_PANEL,
                fg=COLOR_TEXT if row < 2 else COLOR_MUTED,
                anchor="w",
                justify="left",
                wraplength=900,
            ).grid(row=row, column=0, sticky="ew", pady=2)

        controls = tk.Frame(body, bg=COLOR_PANEL)
        controls.grid(row=0, column=1, sticky="ne", padx=(18, 0))

        self.connect_button = self._button(
            controls,
            "Connect",
            self.connect_async,
            COLOR_BLUE,
        )
        self.connect_button.grid(row=0, column=0, padx=4, pady=4)

        self.disconnect_button = self._button(
            controls,
            "Disconnect",
            self.disconnect_async,
            COLOR_DISABLED,
        )
        self.disconnect_button.grid(row=0, column=1, padx=4, pady=4)
        self.disconnect_button.configure(state="disabled")

        self.refresh_status_button = self._button(
            controls,
            "Read Status",
            self.read_status_async,
            COLOR_PANEL_ALT,
        )
        self.refresh_status_button.grid(row=1, column=0, padx=4, pady=4)
        self.refresh_status_button.configure(state="disabled")

        self.local_button = self._button(
            controls,
            "Return LOCAL",
            self.return_local_async,
            COLOR_SUCCESS,
        )
        self.local_button.grid(row=1, column=1, padx=4, pady=4)
        self.local_button.configure(state="disabled")

    def _build_measurement_panel(self, parent: tk.Misc) -> None:
        _, body = self._panel(
            parent,
            "2. Measurement Function",
            row=1,
            column=0,
            columnspan=2,
        )

        groups: dict[str, list[MeasurementDefinition]] = {}
        for definition in MEASUREMENTS:
            groups.setdefault(definition.group, []).append(definition)

        group_names = list(groups)
        for index, group_name in enumerate(group_names):
            group_frame = tk.Frame(
                body,
                bg=COLOR_PANEL_ALT,
                highlightbackground=COLOR_BORDER,
                highlightthickness=1,
            )
            group_frame.grid(
                row=index // 3,
                column=index % 3,
                sticky="nsew",
                padx=6,
                pady=6,
            )
            body.grid_columnconfigure(index % 3, weight=1)

            tk.Label(
                group_frame,
                text=group_name,
                bg=COLOR_PANEL_ALT,
                fg=COLOR_MUTED,
                font=(FONT_FAMILY, 10, "bold"),
                anchor="w",
                padx=10,
                pady=8,
            ).pack(fill="x")

            for definition in groups[group_name]:
                button = ttk.Radiobutton(
                    group_frame,
                    text=definition.name,
                    variable=self.selected_measurement,
                    value=definition.key,
                    style="Function.TRadiobutton",
                    command=self.on_measurement_changed,
                )
                button.pack(fill="x", padx=7, pady=3)
                self.measurement_buttons.append(button)
                self.configuration_widgets.append(button)

        note_frame = tk.Frame(body, bg=COLOR_PANEL)
        note_frame.grid(
            row=2,
            column=0,
            columnspan=3,
            sticky="ew",
            padx=6,
            pady=(10, 0),
        )

        tk.Label(
            note_frame,
            text="Connection guidance:",
            bg=COLOR_PANEL,
            fg=COLOR_MUTED,
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(anchor="w")

        tk.Label(
            note_frame,
            textvariable=self.connection_note_var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            justify="left",
            anchor="w",
            wraplength=1200,
        ).pack(fill="x", pady=(3, 0))

    def _build_acquisition_panel(self, parent: tk.Misc) -> None:
        _, body = self._panel(
            parent,
            "3. Acquisition, Limits, and Recording",
            row=2,
            column=0,
        )
        body.grid_columnconfigure(1, weight=1)

        tk.Label(
            body,
            text="Sample interval",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
        ).grid(row=0, column=0, sticky="w", pady=5)

        self.sample_interval_combo = ttk.Combobox(
            body,
            textvariable=self.sample_interval_var,
            values=(
                "0.1",
                "0.2",
                "0.5",
                "1.0",
                "2.0",
                "5.0",
                "10.0",
                "30.0",
                "60.0",
            ),
            state="normal",
            width=13,
        )
        self.sample_interval_combo.grid(
            row=0,
            column=1,
            sticky="w",
            padx=(12, 5),
            pady=5,
        )
        self.configuration_widgets.append(self.sample_interval_combo)

        tk.Label(
            body,
            text="seconds between requested readings",
            bg=COLOR_PANEL,
            fg=COLOR_MUTED,
        ).grid(row=0, column=2, sticky="w", pady=5)

        tk.Label(
            body,
            text="Graph mode",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
        ).grid(row=1, column=0, sticky="w", pady=5)

        self.graph_mode_combo = ttk.Combobox(
            body,
            textvariable=self.graph_mode_var,
            values=("Rolling", "Static"),
            state="readonly",
            width=13,
        )
        self.graph_mode_combo.grid(
            row=1,
            column=1,
            sticky="w",
            padx=(12, 5),
            pady=5,
        )
        self.graph_mode_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._graph_mode_changed(),
        )
        self.configuration_widgets.append(self.graph_mode_combo)

        tk.Label(
            body,
            text="Rolling points",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
        ).grid(row=2, column=0, sticky="w", pady=5)

        self.rolling_points_combo = ttk.Combobox(
            body,
            textvariable=self.rolling_points_var,
            values=("50", "100", "300", "500", "1000", "5000"),
            state="normal",
            width=13,
        )
        self.rolling_points_combo.grid(
            row=2,
            column=1,
            sticky="w",
            padx=(12, 5),
            pady=5,
        )
        self.configuration_widgets.append(self.rolling_points_combo)

        limits_box = tk.Frame(
            body,
            bg=COLOR_PANEL_ALT,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
        )
        limits_box.grid(
            row=3,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(12, 8),
        )
        limits_box.grid_columnconfigure(1, weight=1)
        limits_box.grid_columnconfigure(3, weight=1)

        tk.Label(
            limits_box,
            text="Optional warning limits",
            bg=COLOR_PANEL_ALT,
            fg=COLOR_MUTED,
            font=(FONT_FAMILY, 9, "bold"),
        ).grid(
            row=0,
            column=0,
            columnspan=4,
            sticky="w",
            padx=10,
            pady=(8, 4),
        )

        tk.Label(
            limits_box,
            text="Low",
            bg=COLOR_PANEL_ALT,
            fg=COLOR_TEXT,
        ).grid(row=1, column=0, sticky="w", padx=(10, 6), pady=(2, 9))

        self.low_limit_entry = ttk.Entry(
            limits_box,
            textvariable=self.low_limit_var,
            style="Dark.TEntry",
            width=16,
        )
        self.low_limit_entry.grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(0, 12),
            pady=(2, 9),
        )
        self.configuration_widgets.append(self.low_limit_entry)

        tk.Label(
            limits_box,
            text="High",
            bg=COLOR_PANEL_ALT,
            fg=COLOR_TEXT,
        ).grid(row=1, column=2, sticky="w", padx=(0, 6), pady=(2, 9))

        self.high_limit_entry = ttk.Entry(
            limits_box,
            textvariable=self.high_limit_var,
            style="Dark.TEntry",
            width=16,
        )
        self.high_limit_entry.grid(
            row=1,
            column=3,
            sticky="ew",
            padx=(0, 10),
            pady=(2, 9),
        )
        self.configuration_widgets.append(self.high_limit_entry)

        csv_box = tk.Frame(
            body,
            bg=COLOR_PANEL_ALT,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
        )
        csv_box.grid(
            row=4,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(5, 8),
        )
        csv_box.grid_columnconfigure(0, weight=1)

        self.record_checkbutton = ttk.Checkbutton(
            csv_box,
            text="Record measurements to CSV",
            variable=self.record_csv_var,
            style="Dark.TCheckbutton",
            command=self.on_record_option_changed,
        )
        self.record_checkbutton.grid(
            row=0,
            column=0,
            sticky="w",
            padx=9,
            pady=7,
        )
        self.configuration_widgets.append(self.record_checkbutton)

        self.choose_csv_button = self._button(
            csv_box,
            "Choose CSV",
            self.choose_csv_file,
            COLOR_PANEL_ALT,
            width=12,
        )
        self.choose_csv_button.grid(
            row=0,
            column=1,
            padx=8,
            pady=6,
        )
        self.configuration_widgets.append(self.choose_csv_button)

        tk.Label(
            csv_box,
            textvariable=self.csv_path_var,
            bg=COLOR_PANEL_ALT,
            fg=COLOR_TEXT,
            anchor="w",
            justify="left",
            wraplength=650,
        ).grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=10,
            pady=(0, 4),
        )

        self.csv_state_label = tk.Label(
            csv_box,
            textvariable=self.csv_status_var,
            bg=COLOR_PANEL_ALT,
            fg=COLOR_MUTED,
            anchor="w",
            padx=10,
            pady=5,
        )
        self.csv_state_label.grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="ew",
        )

        controls = tk.Frame(body, bg=COLOR_PANEL)
        controls.grid(
            row=5,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(10, 0),
        )

        self.start_button = self._button(
            controls,
            "Start",
            self.start_acquisition,
            COLOR_LIVE,
        )
        self.start_button.grid(row=0, column=0, padx=4, pady=4)
        self.start_button.configure(state="disabled")

        self.stop_button = self._button(
            controls,
            "Stop",
            self.stop_acquisition,
            COLOR_DANGER,
        )
        self.stop_button.grid(row=0, column=1, padx=4, pady=4)
        self.stop_button.configure(state="disabled")

        self.single_button = self._button(
            controls,
            "Single Reading",
            self.single_reading,
            COLOR_BLUE,
        )
        self.single_button.grid(row=0, column=2, padx=4, pady=4)
        self.single_button.configure(state="disabled")

        self.clear_button = self._button(
            controls,
            "Clear Graph",
            self.clear_graph,
            COLOR_PANEL_ALT,
        )
        self.clear_button.grid(row=0, column=3, padx=4, pady=4)

    def _build_advanced_panel(self, parent: tk.Misc) -> None:
        _, body = self._panel(
            parent,
            "4. Function-Specific Configuration",
            row=2,
            column=1,
        )
        body.grid_columnconfigure(1, weight=1)

        tk.Label(
            body,
            textvariable=self.advanced_title_var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            font=(FONT_FAMILY, 11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        tk.Label(
            body,
            text="Diode configuration",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
        ).grid(row=1, column=0, sticky="w", pady=5)

        self.diode_combo = ttk.Combobox(
            body,
            textvariable=self.diode_mode_var,
            values=(
                "Default",
                "1 mA / 5 V",
                "0.1 mA / 5 V",
                "1 mA / 10 V",
                "0.1 mA / 10 V",
            ),
            state="disabled",
            width=22,
        )
        self.diode_combo.grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(12, 0),
            pady=5,
        )
        self.configuration_widgets.append(self.diode_combo)

        tk.Label(
            body,
            text="RTD type",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
        ).grid(row=2, column=0, sticky="w", pady=5)

        self.rtd_type_combo = ttk.Combobox(
            body,
            textvariable=self.rtd_type_var,
            values=("PT100_385", "PT100_392", "CUST1"),
            state="disabled",
            width=22,
        )
        self.rtd_type_combo.grid(
            row=2,
            column=1,
            sticky="ew",
            padx=(12, 0),
            pady=5,
        )
        self.rtd_type_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._update_advanced_controls(),
        )
        self.configuration_widgets.append(self.rtd_type_combo)

        tk.Label(
            body,
            text="Custom RTD R0",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
        ).grid(row=3, column=0, sticky="w", pady=5)

        self.rtd_r0_entry = ttk.Entry(
            body,
            textvariable=self.rtd_r0_var,
            style="Dark.TEntry",
            state="disabled",
        )
        self.rtd_r0_entry.grid(
            row=3,
            column=1,
            sticky="ew",
            padx=(12, 0),
            pady=5,
        )
        self.configuration_widgets.append(self.rtd_r0_entry)

        tk.Label(
            body,
            text="Custom RTD alpha",
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
        ).grid(row=4, column=0, sticky="w", pady=5)

        self.rtd_alpha_entry = ttk.Entry(
            body,
            textvariable=self.rtd_alpha_var,
            style="Dark.TEntry",
            state="disabled",
        )
        self.rtd_alpha_entry.grid(
            row=4,
            column=1,
            sticky="ew",
            padx=(12, 0),
            pady=5,
        )
        self.configuration_widgets.append(self.rtd_alpha_entry)

        tk.Label(
            body,
            text=(
                "For functions without special settings, the DMM4050 uses "
                "the SCPI MEASure? command with automatic range selection."
            ),
            bg=COLOR_PANEL,
            fg=COLOR_MUTED,
            justify="left",
            anchor="w",
            wraplength=520,
        ).grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(14, 0),
        )

        self._update_advanced_controls()

    def _build_live_panel(self, parent: tk.Misc) -> None:
        _, body = self._panel(
            parent,
            "5. Live Measurement",
            row=3,
            column=0,
            columnspan=2,
        )
        body.grid_columnconfigure(0, weight=1)

        self.live_state_label = tk.Label(
            body,
            text="READY",
            bg=COLOR_BLUE,
            fg=COLOR_TEXT,
            font=(FONT_FAMILY, 10, "bold"),
            padx=14,
            pady=7,
        )
        self.live_state_label.grid(row=0, column=0, pady=(0, 10))

        tk.Label(
            body,
            textvariable=self.function_var,
            bg=COLOR_PANEL,
            fg=COLOR_MUTED,
            font=(FONT_FAMILY, 11, "bold"),
        ).grid(row=1, column=0)

        self.live_value_label = tk.Label(
            body,
            textvariable=self.live_value_var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            font=(FONT_FAMILY, 38, "bold"),
            padx=10,
            pady=8,
        )
        self.live_value_label.grid(row=2, column=0, sticky="ew")

        detail_frame = tk.Frame(body, bg=COLOR_PANEL)
        detail_frame.grid(row=3, column=0, sticky="ew", pady=(5, 0))

        for column in range(4):
            detail_frame.grid_columnconfigure(column, weight=1)

        detail_vars = (
            self.raw_value_var,
            self.timestamp_var,
            self.rate_var,
            self.count_var,
        )

        for index, variable in enumerate(detail_vars):
            tk.Label(
                detail_frame,
                textvariable=variable,
                bg=COLOR_PANEL_ALT,
                fg=COLOR_TEXT,
                anchor="center",
                padx=8,
                pady=8,
                highlightbackground=COLOR_BORDER,
                highlightthickness=1,
            ).grid(
                row=0,
                column=index,
                sticky="ew",
                padx=4,
                pady=4,
            )

    def _build_graph_panel(self, parent: tk.Misc) -> None:
        _, body = self._panel(
            parent,
            "6. Measurement Graph",
            row=4,
            column=0,
            columnspan=2,
        )

        graph_information = tk.Frame(
            body,
            bg=COLOR_PANEL_ALT,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
        )
        graph_information.pack(fill="x", pady=(0, 10))

        tk.Label(
            graph_information,
            textvariable=self.graph_scale_var,
            bg=COLOR_PANEL_ALT,
            fg=COLOR_TEXT,
            anchor="w",
            padx=10,
            pady=7,
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(fill="x")

        tk.Label(
            graph_information,
            text=(
                "The graph automatically converts base SI readings into "
                "readable engineering units. CSV data remain in the original "
                "DMM base unit."
            ),
            bg=COLOR_PANEL_ALT,
            fg=COLOR_MUTED,
            anchor="w",
            justify="left",
            padx=10,
            pady=0,
            wraplength=1200,
        ).pack(fill="x", pady=(0, 7))

        self.figure = Figure(
            figsize=(12, 4.5),
            dpi=100,
            facecolor=COLOR_PANEL,
        )
        self.axis = self.figure.add_subplot(111)
        self._style_axis("Time (s)", "Value")

        self.graph_canvas = FigureCanvasTkAgg(self.figure, master=body)
        self.graph_canvas.draw()
        self.graph_canvas.get_tk_widget().configure(
            bg=COLOR_PANEL,
            highlightthickness=0,
        )
        self.graph_canvas.get_tk_widget().pack(fill="both", expand=True)

    def _build_log_panel(self, parent: tk.Misc) -> None:
        _, body = self._panel(
            parent,
            "7. Event Log",
            row=5,
            column=0,
            columnspan=2,
        )

        text_frame = tk.Frame(body, bg=COLOR_PANEL)
        text_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            text_frame,
            height=10,
            bg=COLOR_ENTRY,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            selectbackground=COLOR_BLUE,
            selectforeground=COLOR_TEXT,
            wrap="word",
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
            font=("Consolas", 9),
            state="disabled",
        )
        log_scroll = ttk.Scrollbar(
            text_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self.log_message("Application started.")

    # ------------------------------------------------------------------
    # Connection actions
    # ------------------------------------------------------------------

    def connect_async(self) -> None:
        if self.dmm.connected:
            return

        if self.connect_thread and self.connect_thread.is_alive():
            return

        self.connect_button.configure(state="disabled")
        self._set_connection_state("CONNECTING", COLOR_WARNING)
        self.footer_status.configure(
            text="Connecting to the DMM4050 at GPIB address 10..."
        )
        self.log_message("Connecting to the DMM4050.")

        self.connect_thread = threading.Thread(
            target=self._connect_worker,
            name="DMM4050-Connect",
            daemon=True,
        )
        self.connect_thread.start()

    def _connect_worker(self) -> None:
        try:
            status = self.dmm.connect()
            self.event_queue.put(("connected", status))
        except Exception as exc:
            self.event_queue.put(
                (
                    "connection_error",
                    {
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )
            )

    def disconnect_async(self) -> None:
        if self.running:
            messagebox.showwarning(
                "Measurement Running",
                "Stop the measurement before disconnecting the DMM4050.",
                parent=self,
            )
            return

        self.disconnect_button.configure(state="disabled")
        self._set_connection_state("DISCONNECTING", COLOR_WARNING)
        self.log_message("Disconnecting the DMM4050.")

        thread = threading.Thread(
            target=self._disconnect_worker,
            name="DMM4050-Disconnect",
            daemon=True,
        )
        thread.start()

    def _disconnect_worker(self) -> None:
        try:
            local_ok, local_message = self.dmm.close(go_local=True)
            self.event_queue.put(
                (
                    "disconnected",
                    {
                        "local_ok": local_ok,
                        "local_message": local_message,
                    },
                )
            )
        except Exception as exc:
            self.event_queue.put(("disconnect_error", str(exc)))

    def return_local_async(self) -> None:
        if not self.dmm.connected:
            return

        self.log_message("Sending IEEE-488 Go To Local.")

        def worker() -> None:
            ok, message = self.dmm.return_to_local()
            self.event_queue.put(
                (
                    "local_result",
                    {
                        "ok": ok,
                        "message": message,
                    },
                )
            )

        threading.Thread(
            target=worker,
            name="DMM4050-Local",
            daemon=True,
        ).start()

    def read_status_async(self) -> None:
        if not self.dmm.connected or self.running:
            return

        self.refresh_status_button.configure(state="disabled")

        def worker() -> None:
            try:
                status = self.dmm.read_status()
                self.event_queue.put(("status_read", status))
            except Exception as exc:
                self.event_queue.put(("status_error", str(exc)))

        threading.Thread(
            target=worker,
            name="DMM4050-Status",
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Measurement setup and controls
    # ------------------------------------------------------------------

    def on_measurement_changed(self) -> None:
        definition = MEASUREMENT_BY_KEY[self.selected_measurement.get()]
        self.function_var.set(f"Function: {definition.name}")
        self.connection_note_var.set(definition.connection_note)
        self._update_advanced_controls()

        # Prevent data from a previous function from being displayed with
        # the newly selected function name and unit.
        if not self.running:
            self.elapsed_data.clear()
            self.value_data.clear()
            self.last_sample_times.clear()
            self.sample_count = 0
            self.count_var.set("Samples: 0")
            self.rate_var.set("Actual rate: --")
            self.live_value_var.set("No reading")
            self.raw_value_var.set("Raw value: --")
            self.timestamp_var.set("Timestamp: --")
            self.live_value_label.configure(fg=COLOR_TEXT)

        self.graph_scale_var.set(
            f"Y-axis: {definition.name} ({definition.unit}); "
            "waiting for measurement data"
        )
        self.graph_dirty = True

    def _update_advanced_controls(self) -> None:
        definition = MEASUREMENT_BY_KEY[self.selected_measurement.get()]
        is_diode = definition.special_setup == "diode"
        is_rtd = definition.special_setup in {"rtd2", "rtd4"}
        is_custom_rtd = is_rtd and self.rtd_type_var.get() == "CUST1"

        if self.running:
            diode_state = "disabled"
            rtd_state = "disabled"
            custom_state = "disabled"
        else:
            diode_state = "readonly" if is_diode else "disabled"
            rtd_state = "readonly" if is_rtd else "disabled"
            custom_state = "normal" if is_custom_rtd else "disabled"

        self.diode_combo.configure(state=diode_state)
        self.rtd_type_combo.configure(state=rtd_state)
        self.rtd_r0_entry.configure(state=custom_state)
        self.rtd_alpha_entry.configure(state=custom_state)

        if is_diode:
            self.advanced_title_var.set("Diode Test Configuration")
        elif is_rtd:
            self.advanced_title_var.set("RTD Temperature Configuration")
        else:
            self.advanced_title_var.set("Automatic Measurement Configuration")

    def _build_measurement_plan(self) -> MeasurementPlan:
        definition = MEASUREMENT_BY_KEY[self.selected_measurement.get()]
        command = definition.command

        if definition.special_setup == "diode":
            diode_commands = {
                "Default": "MEASure:DIODe?",
                "1 mA / 5 V": "MEASure:DIODe? OFF,OFF",
                "0.1 mA / 5 V": "MEASure:DIODe? ON,OFF",
                "1 mA / 10 V": "MEASure:DIODe? OFF,ON",
                "0.1 mA / 10 V": "MEASure:DIODe? ON,ON",
            }
            command = diode_commands[self.diode_mode_var.get()]

        elif definition.special_setup in {"rtd2", "rtd4"}:
            rtd_type = self.rtd_type_var.get()
            subfunction = (
                "RTD"
                if definition.special_setup == "rtd2"
                else "FRTD"
            )

            if rtd_type == "CUST1":
                r0 = safe_float(self.rtd_r0_var.get())
                alpha = safe_float(self.rtd_alpha_var.get())

                if r0 is None or r0 <= 0:
                    raise ValueError("Custom RTD R0 must be greater than zero.")
                if alpha is None or alpha <= 0:
                    raise ValueError(
                        "Custom RTD alpha must be greater than zero."
                    )

                self.dmm.write(
                    f"TEMPerature:{subfunction}:TYPe CUST1"
                )
                self.dmm.write(
                    f"TEMPerature:{subfunction}:R0 {r0:.12g}"
                )
                self.dmm.write(
                    f"TEMPerature:{subfunction}:ALPHa {alpha:.12g}"
                )

            command = f"{definition.command} {rtd_type}"

        # Common return for every measurement type, including functions with
        # no special setup such as DC voltage, current, and resistance.
        return MeasurementPlan(
            key=definition.key,
            name=definition.name,
            command=command,
            unit=definition.unit,
            connection_note=definition.connection_note,
        )

    def _validated_acquisition_settings(
        self,
    ) -> tuple[float, int, float | None, float | None, Path | None]:
        try:
            interval = float(self.sample_interval_var.get().strip())
        except ValueError as exc:
            raise ValueError("Sample interval must be a valid number.") from exc

        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Sample interval must be greater than zero.")

        try:
            rolling_points = int(self.rolling_points_var.get().strip())
        except ValueError as exc:
            raise ValueError(
                "Rolling graph points must be a whole number."
            ) from exc

        if rolling_points < 10:
            raise ValueError("Rolling graph points must be at least 10.")

        low = safe_float(self.low_limit_var.get())
        high = safe_float(self.high_limit_var.get())

        if low is not None and high is not None and low >= high:
            raise ValueError(
                "The low warning limit must be less than the high limit."
            )

        csv_path: Path | None = None
        if self.record_csv_var.get():
            path_text = self.csv_path_var.get().strip()
            if not path_text:
                self.choose_csv_file()
                path_text = self.csv_path_var.get().strip()

            if not path_text:
                raise ValueError(
                    "CSV recording is enabled, but no output file was selected."
                )

            csv_path = Path(path_text).expanduser().resolve()

        return interval, rolling_points, low, high, csv_path

    def start_acquisition(self) -> None:
        self._start_measurement(single=False)

    def single_reading(self) -> None:
        self._start_measurement(single=True)

    def _start_measurement(self, single: bool) -> None:
        if not self.dmm.connected:
            messagebox.showerror(
                "DMM Not Connected",
                "Connect to the DMM4050 before starting a measurement.",
                parent=self,
            )
            return

        if self.running:
            return

        try:
            interval, rolling_points, low, high, csv_path = (
                self._validated_acquisition_settings()
            )
            plan = self._build_measurement_plan()
        except Exception as exc:
            messagebox.showerror(
                "Invalid Configuration",
                str(exc),
                parent=self,
            )
            return

        self.clear_graph()
        self.running = True
        self.stop_event.clear()
        self.session_start_monotonic = time.monotonic()
        self.sample_count = 0
        self.last_sample_times.clear()

        self._set_running_controls(True)
        self._set_run_state(
            "SINGLE READING" if single else "RUNNING",
            COLOR_LIVE,
        )
        self._set_live_state("LIVE", COLOR_LIVE)
        self.function_var.set(f"Function: {plan.name}")
        self.footer_status.configure(
            text=(
                f"Measuring {plan.name}. Requested interval: "
                f"{interval:g} s."
            )
        )

        record_text = str(csv_path) if csv_path else "disabled"
        self.log_message(
            f"Measurement started: {plan.name}; command={plan.command}; "
            f"interval={interval:g} s; CSV={record_text}."
        )

        self.worker_thread = threading.Thread(
            target=self._measurement_worker,
            args=(
                plan,
                interval,
                rolling_points,
                low,
                high,
                csv_path,
                single,
            ),
            name="DMM4050-Acquisition",
            daemon=True,
        )
        self.worker_thread.start()

    def stop_acquisition(self) -> None:
        if not self.running:
            return

        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self._set_run_state("STOPPING", COLOR_WARNING)
        self._set_live_state("STOPPING", COLOR_WARNING)
        self.footer_status.configure(text="Stopping measurement...")
        self.log_message("Stop requested.")

    def _measurement_worker(
        self,
        plan: MeasurementPlan,
        interval: float,
        rolling_points: int,
        low_limit: float | None,
        high_limit: float | None,
        csv_path: Path | None,
        single: bool,
    ) -> None:
        csv_file = None
        csv_writer = None
        saved_path: str | None = None
        stop_reason = "Completed" if single else "Stopped"
        worker_sample_count = 0

        try:
            if csv_path is not None:
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                csv_file = csv_path.open(
                    "w",
                    newline="",
                    encoding="utf-8-sig",
                )
                csv_writer = csv.writer(csv_file)

                csv_writer.writerow(["Instrument", self.dmm.identity])
                csv_writer.writerow(["VISA Resource", self.dmm.resource_name])
                csv_writer.writerow(
                    ["GPIB Address", TARGET_GPIB_ADDRESS]
                )
                csv_writer.writerow(["Measurement", plan.name])
                csv_writer.writerow(["SCPI Command", plan.command])
                csv_writer.writerow(
                    ["Requested Interval Seconds", f"{interval:.9g}"]
                )
                csv_writer.writerow(["Low Warning Limit", low_limit])
                csv_writer.writerow(["High Warning Limit", high_limit])
                csv_writer.writerow([])
                csv_writer.writerow(
                    [
                        "Sample",
                        "Timestamp",
                        "Elapsed Seconds",
                        "Raw Value",
                        "Unit",
                        "Formatted Value",
                        "State",
                    ]
                )
                csv_file.flush()

            while not self.stop_event.is_set():
                cycle_start = time.monotonic()
                elapsed = cycle_start - self.session_start_monotonic
                timestamp = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )

                try:
                    value = self.dmm.query_float_cancellable(
                        plan.command,
                        self.stop_event,
                    )
                    formatted = engineering_text(value, plan.unit)

                    if is_overrange(value):
                        measurement_state = "WARNING"
                        status_text = "OVERLOAD / OPEN"
                    elif (
                        low_limit is not None
                        and value < low_limit
                    ) or (
                        high_limit is not None
                        and value > high_limit
                    ):
                        measurement_state = "WARNING"
                        status_text = "OUTSIDE LIMIT"
                    else:
                        measurement_state = "NORMAL"
                        status_text = "LIVE / NORMAL"

                    worker_sample_count += 1

                    self.event_queue.put(
                        (
                            "reading",
                            {
                                "plan": plan,
                                "elapsed": elapsed,
                                "timestamp": timestamp,
                                "value": value,
                                "formatted": formatted,
                                "measurement_state": measurement_state,
                                "status_text": status_text,
                                "rolling_points": rolling_points,
                                "sample_monotonic": cycle_start,
                            },
                        )
                    )

                    if csv_writer is not None:
                        csv_writer.writerow(
                            [
                                worker_sample_count,
                                timestamp,
                                f"{elapsed:.9f}",
                                f"{value:.12g}",
                                plan.unit,
                                formatted,
                                measurement_state,
                            ]
                        )
                        csv_file.flush()

                except MeasurementCancelled:
                    stop_reason = "Stopped by user"
                    break

                except DMMCommunicationError as exc:
                    # Do not issue another SCPI query after a transport fault.
                    # A delayed measurement response could otherwise be
                    # mistaken for the SYSTem:ERRor? response.
                    self.event_queue.put(
                        (
                            "measurement_error",
                            {
                                "message": str(exc),
                                "dmm_error": (
                                    "Not queried after a VISA transport fault"
                                ),
                                "traceback": traceback.format_exc(),
                            },
                        )
                    )
                    stop_reason = "Communication fault"
                    break

                except Exception as exc:
                    try:
                        dmm_error = self.dmm.check_error()
                    except Exception:
                        dmm_error = "Unavailable"

                    self.event_queue.put(
                        (
                            "measurement_error",
                            {
                                "message": str(exc),
                                "dmm_error": dmm_error,
                                "traceback": traceback.format_exc(),
                            },
                        )
                    )
                    stop_reason = "Measurement fault"
                    break

                if single:
                    stop_reason = "Single reading completed"
                    break

                cycle_duration = time.monotonic() - cycle_start
                delay = max(0.0, interval - cycle_duration)
                if self.stop_event.wait(delay):
                    stop_reason = "Stopped by user"
                    break

        except Exception as exc:
            stop_reason = "Acquisition fault"
            self.event_queue.put(
                (
                    "measurement_error",
                    {
                        "message": str(exc),
                        "dmm_error": "Unavailable",
                        "traceback": traceback.format_exc(),
                    },
                )
            )

        finally:
            if csv_file is not None:
                try:
                    csv_file.flush()
                    os.fsync(csv_file.fileno())
                    csv_file.close()
                    saved_path = str(csv_path)
                except Exception as exc:
                    try:
                        csv_file.close()
                    except Exception:
                        pass
                    self.event_queue.put(
                        ("csv_error", f"CSV finalization failed: {exc}")
                    )

            self.event_queue.put(
                (
                    "acquisition_stopped",
                    {
                        "reason": stop_reason,
                        "saved_path": saved_path,
                    },
                )
            )

    # ------------------------------------------------------------------
    # CSV and graph actions
    # ------------------------------------------------------------------

    def on_record_option_changed(self) -> None:
        if self.record_csv_var.get():
            self.csv_status_var.set("CSV recording enabled")
            self.csv_state_label.configure(fg=COLOR_SUCCESS)

            if not self.csv_path_var.get().strip():
                self.choose_csv_file()
        else:
            self.csv_status_var.set("CSV recording disabled")
            self.csv_state_label.configure(fg=COLOR_MUTED)

    def choose_csv_file(self) -> None:
        definition = MEASUREMENT_BY_KEY[self.selected_measurement.get()]
        default_name = (
            f"DMM4050_{definition.name.replace(' ', '_').replace(',', '')}_"
            f"{datetime.now():%Y%m%d_%H%M%S}.csv"
        )

        filename = filedialog.asksaveasfilename(
            parent=self,
            title="Select CSV output file",
            defaultextension=".csv",
            filetypes=(
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ),
            initialfile=default_name,
            initialdir=str(Path.cwd()),
        )

        if filename:
            self.csv_path_var.set(filename)
            self.record_csv_var.set(True)
            self.csv_status_var.set("CSV file selected")
            self.csv_state_label.configure(fg=COLOR_SUCCESS)
            self.log_message(f"CSV output selected: {filename}")

    def clear_graph(self) -> None:
        self.elapsed_data.clear()
        self.value_data.clear()
        self.last_sample_times.clear()
        self.sample_count = 0
        self.count_var.set("Samples: 0")
        self.rate_var.set("Actual rate: --")

        definition = MEASUREMENT_BY_KEY[self.selected_measurement.get()]
        self.graph_scale_var.set(
            f"Y-axis: {definition.name} ({definition.unit}); "
            "waiting for measurement data"
        )

        self.graph_dirty = True
        self.log_message("Graph and session data cleared.")

    def _graph_mode_changed(self) -> None:
        self.graph_dirty = True

    def _refresh_graph_if_needed(self) -> None:
        if self.closing:
            return

        if self.graph_dirty:
            self._draw_graph()
            self.graph_dirty = False

        self.after(250, self._refresh_graph_if_needed)

    def _draw_graph(self) -> None:
        definition = MEASUREMENT_BY_KEY[self.selected_measurement.get()]
        # Both deques are owned by the Tk main thread. Create immutable
        # snapshots before slicing and plotting so Matplotlib receives stable
        # data for the complete draw operation.
        x_values = list(self.elapsed_data)
        y_values = list(self.value_data)

        if self.graph_mode_var.get() == "Rolling":
            try:
                points = max(10, int(self.rolling_points_var.get()))
            except ValueError:
                points = 300

            x_values = x_values[-points:]
            y_values = y_values[-points:]

        finite_points = [
            (x_value, y_value)
            for x_value, y_value in zip(x_values, y_values)
            if math.isfinite(y_value)
            and not is_overrange(y_value)
        ]

        raw_values = [
            raw_value
            for _elapsed, raw_value in finite_points
        ]
        scale = select_graph_scale(raw_values, definition.unit)
        y_axis_label = (
            f"{definition.name} ({scale.display_unit})"
        )

        self.graph_scale_var.set(
            f"Y-axis: {y_axis_label}. "
            f"Raw DMM and CSV unit: {definition.unit}."
        )

        self.axis.clear()
        self._style_axis("Time (s)", y_axis_label)
        self.axis.set_title(
            (
                f"{definition.name} | "
                f"{self.graph_mode_var.get()} graph | "
                f"Displayed in {scale.display_unit}"
            ),
            color=COLOR_TEXT,
            fontsize=11,
            pad=12,
        )

        if finite_points:
            x_plot = [
                elapsed
                for elapsed, _raw_value in finite_points
            ]
            y_plot = [
                raw_value * scale.multiplier
                for _elapsed, raw_value in finite_points
            ]

            self.axis.plot(
                x_plot,
                y_plot,
                linewidth=1.6,
                color=COLOR_GRAPH_LINE,
            )

            self.axis.scatter(
                [x_plot[-1]],
                [y_plot[-1]],
                s=30,
                color=COLOR_SUCCESS,
                zorder=3,
            )

            self.axis.margins(x=0.02, y=0.12)
        else:
            self.axis.text(
                0.5,
                0.5,
                "No valid measurement samples",
                transform=self.axis.transAxes,
                ha="center",
                va="center",
                color=COLOR_MUTED,
                fontsize=12,
            )

        self.axis.yaxis.set_major_formatter(
            FuncFormatter(format_plain_axis_tick)
        )
        self.axis.xaxis.set_major_formatter(
            FuncFormatter(format_plain_axis_tick)
        )

        self.figure.tight_layout(pad=1.5)
        self.graph_canvas.draw_idle()

    def _style_axis(self, x_label: str, y_label: str) -> None:
        self.axis.set_facecolor(COLOR_ENTRY)
        self.axis.set_xlabel(x_label, color=COLOR_TEXT)
        self.axis.set_ylabel(y_label, color=COLOR_TEXT)
        self.axis.tick_params(
            axis="both",
            colors=COLOR_MUTED,
            labelsize=9,
        )
        self.axis.grid(
            True,
            alpha=0.28,
            color=COLOR_BORDER,
            linewidth=0.7,
        )

        # Prevent Matplotlib from showing multipliers such as "1e-6".
        self.axis.ticklabel_format(
            axis="both",
            style="plain",
            useOffset=False,
        )

        for spine in self.axis.spines.values():
            spine.set_color(COLOR_BORDER)

    # ------------------------------------------------------------------
    # Queue handling and UI state
    # ------------------------------------------------------------------

    def _process_event_queue(self) -> None:
        if self.closing:
            return

        while True:
            try:
                event_name, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if event_name == "connected":
                self._handle_connected(payload)
            elif event_name == "connection_error":
                self._handle_connection_error(payload)
            elif event_name == "disconnected":
                self._handle_disconnected(payload)
            elif event_name == "disconnect_error":
                self._handle_disconnect_error(str(payload))
            elif event_name == "local_result":
                self._handle_local_result(payload)
            elif event_name == "status_read":
                self._handle_status_read(payload)
            elif event_name == "status_error":
                self._handle_status_error(str(payload))
            elif event_name == "reading":
                self._handle_reading(payload)
            elif event_name == "measurement_error":
                self._handle_measurement_error(payload)
            elif event_name == "csv_error":
                self._handle_csv_error(str(payload))
            elif event_name == "acquisition_stopped":
                self._handle_acquisition_stopped(payload)

        self.after(100, self._process_event_queue)

    def _handle_connected(self, status: object) -> None:
        assert isinstance(status, dict)

        self._set_connection_state("CONNECTED", COLOR_SUCCESS)
        self.identity_var.set(
            f"Instrument: {status.get('Identity', 'Unknown')}"
        )
        self.resource_var.set(
            f"Resource: {status.get('Resource', 'Unknown')}"
        )
        self.terminal_var.set(
            f"Input terminals: {status.get('Input Terminals', 'Unavailable')}"
        )
        self.dmm_error_var.set(
            f"DMM error queue: {status.get('Error Queue', 'Unavailable')}"
        )

        self.connect_button.configure(state="disabled")
        self.disconnect_button.configure(state="normal")
        self.refresh_status_button.configure(state="normal")
        self.local_button.configure(state="normal")
        self.start_button.configure(state="normal")
        self.single_button.configure(state="normal")

        self.footer_status.configure(
            text="DMM4050 connected and ready for measurement."
        )
        self.log_message(
            f"Connected: {status.get('Identity', 'Unknown')}."
        )

    def _handle_connection_error(self, payload: object) -> None:
        assert isinstance(payload, dict)

        self._set_connection_state("FAULT", COLOR_DANGER)
        self.connect_button.configure(state="normal")
        self.disconnect_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.single_button.configure(state="disabled")

        message = str(payload.get("message", "Unknown connection error"))
        self.footer_status.configure(text=f"Connection failed: {message}")
        self.log_message(f"Connection fault: {message}")

        messagebox.showerror(
            "DMM4050 Connection Failed",
            message,
            parent=self,
        )

    def _handle_disconnected(self, payload: object) -> None:
        assert isinstance(payload, dict)

        self._set_connection_state("DISCONNECTED", COLOR_DISABLED)
        self.identity_var.set("Instrument: not connected")
        self.resource_var.set("Resource: not connected")
        self.terminal_var.set("Input terminals: unknown")
        self.dmm_error_var.set("DMM error queue: not checked")

        self.connect_button.configure(state="normal")
        self.disconnect_button.configure(state="disabled")
        self.refresh_status_button.configure(state="disabled")
        self.local_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.single_button.configure(state="disabled")

        local_ok = bool(payload.get("local_ok"))
        local_message = str(payload.get("local_message", ""))

        if local_ok:
            self.footer_status.configure(
                text="DMM4050 disconnected. LOCAL control requested."
            )
            self.log_message(f"Disconnected. {local_message}")
        else:
            self.footer_status.configure(
                text=(
                    "Disconnected. Press the LOCAL soft key on the DMM4050 "
                    "if REM is still displayed."
                )
            )
            self.log_message(
                f"Disconnected. Automatic LOCAL failed: {local_message}"
            )
            messagebox.showwarning(
                "Return to LOCAL",
                (
                    "The VISA session is closed, but automatic Go To Local "
                    "was not supported. Press the LOCAL soft key on the "
                    "DMM4050 if REM is still displayed."
                ),
                parent=self,
            )

    def _handle_disconnect_error(self, message: str) -> None:
        self._set_connection_state("FAULT", COLOR_DANGER)
        self.disconnect_button.configure(state="normal")
        self.footer_status.configure(text=f"Disconnect error: {message}")
        self.log_message(f"Disconnect fault: {message}")

    def _handle_local_result(self, payload: object) -> None:
        assert isinstance(payload, dict)

        ok = bool(payload.get("ok"))
        message = str(payload.get("message", ""))

        if ok:
            self.footer_status.configure(
                text="Go To Local command sent to the DMM4050."
            )
            self.log_message(message)
            self._set_live_state("LOCAL REQUESTED", COLOR_SUCCESS)
        else:
            self.footer_status.configure(
                text="Press the LOCAL soft key on the DMM4050."
            )
            self.log_message(message)
            messagebox.showwarning(
                "Return to LOCAL",
                (
                    f"{message}\n\n"
                    "Press the LOCAL soft key on the DMM4050."
                ),
                parent=self,
            )

    def _handle_status_read(self, status: object) -> None:
        assert isinstance(status, dict)

        self.identity_var.set(
            f"Instrument: {status.get('Identity', 'Unknown')}"
        )
        self.resource_var.set(
            f"Resource: {status.get('Resource', 'Unknown')}"
        )
        self.terminal_var.set(
            f"Input terminals: {status.get('Input Terminals', 'Unavailable')}"
        )
        self.dmm_error_var.set(
            f"DMM error queue: {status.get('Error Queue', 'Unavailable')}"
        )
        self.refresh_status_button.configure(state="normal")
        self.footer_status.configure(text="DMM status refreshed.")
        self.log_message(
            "DMM status refreshed. "
            f"Function={status.get('Active Function', 'Unavailable')}."
        )

    def _handle_status_error(self, message: str) -> None:
        self.refresh_status_button.configure(state="normal")
        self.footer_status.configure(text=f"Status read failed: {message}")
        self.log_message(f"Status read fault: {message}")

    def _handle_reading(self, payload: object) -> None:
        assert isinstance(payload, dict)
        plan = payload["plan"]
        assert isinstance(plan, MeasurementPlan)

        elapsed = float(payload["elapsed"])
        value = float(payload["value"])
        timestamp = str(payload["timestamp"])
        formatted = str(payload["formatted"])
        measurement_state = str(payload["measurement_state"])
        status_text = str(payload["status_text"])
        sample_monotonic = float(payload["sample_monotonic"])

        self.sample_count += 1
        self.elapsed_data.append(elapsed)
        self.value_data.append(value)
        self.last_sample_times.append(sample_monotonic)

        self.live_value_var.set(formatted)
        self.raw_value_var.set(f"Raw value: {value:.12g} {plan.unit}")
        self.timestamp_var.set(f"Timestamp: {timestamp}")
        self.count_var.set(f"Samples: {self.sample_count}")

        if len(self.last_sample_times) >= 2:
            duration = (
                self.last_sample_times[-1]
                - self.last_sample_times[0]
            )
            intervals = len(self.last_sample_times) - 1
            if duration > 0:
                rate = intervals / duration
                self.rate_var.set(f"Actual rate: {rate:.3f} samples/s")

        if measurement_state == "WARNING":
            self._set_live_state(status_text, COLOR_WARNING, dark_text=True)
            self.live_value_label.configure(fg=COLOR_WARNING)
        else:
            self._set_live_state(status_text, COLOR_LIVE)
            self.live_value_label.configure(fg=COLOR_TEXT)

        self.graph_dirty = True

    def _handle_measurement_error(self, payload: object) -> None:
        assert isinstance(payload, dict)

        message = str(payload.get("message", "Unknown measurement error"))
        dmm_error = str(payload.get("dmm_error", "Unavailable"))

        self._set_live_state("FAULT / ALARM", COLOR_DANGER)
        self.live_value_label.configure(fg=COLOR_DANGER)
        self.live_value_var.set("Measurement fault")
        self.dmm_error_var.set(f"DMM error queue: {dmm_error}")
        self.footer_status.configure(text=f"Measurement fault: {message}")
        self.log_message(
            f"Measurement fault: {message}; DMM error={dmm_error}"
        )

    def _handle_csv_error(self, message: str) -> None:
        self.csv_status_var.set("CSV recording fault")
        self.csv_state_label.configure(fg=COLOR_DANGER)
        self.log_message(message)
        messagebox.showerror(
            "CSV Recording Fault",
            message,
            parent=self,
        )

    def _handle_acquisition_stopped(self, payload: object) -> None:
        assert isinstance(payload, dict)

        self.running = False
        self.stop_event.set()
        self._set_running_controls(False)

        reason = str(payload.get("reason", "Stopped"))
        saved_path = payload.get("saved_path")

        if saved_path:
            self._set_run_state("SAVED", COLOR_SUCCESS)
            self._set_live_state("SAVED", COLOR_SUCCESS)
            self.csv_status_var.set(f"Saved: {saved_path}")
            self.csv_state_label.configure(fg=COLOR_SUCCESS)
            self.footer_status.configure(
                text=f"{reason}. CSV saved to {saved_path}"
            )
            self.log_message(f"{reason}. CSV saved: {saved_path}")
        else:
            if "fault" in reason.lower():
                self._set_run_state("FAULT", COLOR_DANGER)
            else:
                self._set_run_state("IDLE", COLOR_BLUE)
                self._set_live_state("STOPPED", COLOR_BLUE)

            self.footer_status.configure(text=reason)
            self.log_message(reason)

    def _set_running_controls(self, running: bool) -> None:
        self.start_button.configure(
            state="disabled" if running else "normal"
        )
        self.single_button.configure(
            state="disabled" if running else "normal"
        )
        self.stop_button.configure(
            state="normal" if running else "disabled"
        )
        self.disconnect_button.configure(
            state="disabled" if running else "normal"
        )
        self.refresh_status_button.configure(
            state="disabled" if running else "normal"
        )
        self.local_button.configure(
            state="disabled" if running else "normal"
        )

        state = "disabled" if running else "normal"
        for widget in self.measurement_buttons:
            widget.configure(state=state)

        if running:
            self.sample_interval_combo.configure(state="disabled")
            self.graph_mode_combo.configure(state="disabled")
            self.rolling_points_combo.configure(state="disabled")
            self.low_limit_entry.configure(state="disabled")
            self.high_limit_entry.configure(state="disabled")
            self.record_checkbutton.configure(state="disabled")
            self.choose_csv_button.configure(state="disabled")
        else:
            self.sample_interval_combo.configure(state="normal")
            self.graph_mode_combo.configure(state="readonly")
            self.rolling_points_combo.configure(state="normal")
            self.low_limit_entry.configure(state="normal")
            self.high_limit_entry.configure(state="normal")
            self.record_checkbutton.configure(state="normal")
            self.choose_csv_button.configure(state="normal")

        self._update_advanced_controls()

    def _set_connection_state(self, text: str, color: str) -> None:
        self.connection_state_var.set(text)
        self.connection_badge.configure(bg=color)

    def _set_run_state(self, text: str, color: str) -> None:
        self.run_state_var.set(text)
        self.run_badge.configure(bg=color)

    def _set_live_state(
        self,
        text: str,
        color: str,
        dark_text: bool = False,
    ) -> None:
        self.live_state_label.configure(
            text=text,
            bg=color,
            fg=COLOR_BG if dark_text else COLOR_TEXT,
        )

    def log_message(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # Application shutdown
    # ------------------------------------------------------------------

    def on_close(self) -> None:
        if self.closing:
            return

        if self.running:
            confirmed = messagebox.askyesno(
                "Stop and Exit",
                (
                    "A measurement is running. Stop the measurement, close "
                    "the VISA session, and exit?"
                ),
                parent=self,
            )
            if not confirmed:
                return

        self.closing = True
        self.stop_event.set()
        self.footer_status.configure(
            text="Stopping acquisition and closing the VISA session safely..."
        )
        self._set_run_state("CLOSING", COLOR_WARNING)
        self._set_live_state("CLOSING", COLOR_WARNING)

        def close_worker() -> None:
            try:
                # Never close the VISA session while a measurement query is
                # active. The worker owns the DMM lock during the validated
                # blocking VISA read and releases it before session close.
                worker = self.worker_thread
                if worker is not None and worker.is_alive():
                    worker.join()

                local_ok, local_message = self.dmm.close(go_local=True)
                self.event_queue.put(
                    (
                        "application_closed",
                        {
                            "local_ok": local_ok,
                            "local_message": local_message,
                        },
                    )
                )
            except Exception:
                self.event_queue.put(
                    (
                        "application_closed",
                        {
                            "local_ok": False,
                            "local_message": traceback.format_exc(),
                        },
                    )
                )

        thread = threading.Thread(
            target=close_worker,
            name="DMM4050-Close",
            daemon=True,
        )
        thread.start()

        self.after(100, self._finish_close_when_ready)
        self.after(
            SHUTDOWN_DELAY_WARNING_MS,
            self._show_shutdown_delay_warning,
        )

    def _show_shutdown_delay_warning(self) -> None:
        if not self.closing or not self.winfo_exists():
            return

        worker = self.worker_thread
        if worker is not None and worker.is_alive():
            self.footer_status.configure(
                text=(
                    "Still waiting for the active VISA operation to finish. "
                    "The session has not been closed concurrently."
                )
            )
            messagebox.showwarning(
                "Waiting for DMM4050",
                (
                    "The application is still waiting for the active VISA "
                    "operation to finish safely. The instrument session has "
                    "not been forced closed from another thread.\n\n"
                    "Wait for the operation to complete. If the adapter is "
                    "physically unresponsive, disconnect its USB cable only "
                    "after Windows reports that the application has stopped."
                ),
                parent=self,
            )

    def _finish_close_when_ready(self) -> None:
        try:
            while True:
                event_name, payload = self.event_queue.get_nowait()
                if event_name == "application_closed":
                    if isinstance(payload, dict) and not payload.get(
                        "local_ok", True
                    ):
                        # The GUI is closing, so use a final direct message.
                        messagebox.showwarning(
                            "Return DMM4050 to LOCAL",
                            (
                                "Automatic Go To Local was not supported. "
                                "Press the LOCAL soft key on the DMM4050 if "
                                "REM is still displayed."
                            ),
                            parent=self,
                        )
                    self.destroy()
                    return
        except queue.Empty:
            pass

        self.after(100, self._finish_close_when_ready)


def enable_windows_dpi_awareness() -> None:
    """Enable DPI awareness before the Tk root window is created."""
    if sys.platform != "win32":
        return

    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main() -> None:
    enable_windows_dpi_awareness()
    app = DMM4050App()
    app.mainloop()


if __name__ == "__main__":
    main()
