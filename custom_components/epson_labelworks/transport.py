from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from . import protocol

EPSON_VENDOR_ID = 0x04B8
LABELWORKS_PRODUCT_ID = 0x0705
SUPPORTED_USB_MODEL = "LW-600P"
STATUS_POLL_INTERVAL_S = 1


@dataclass(frozen=True)
class UsbDeviceInfo:
    vendor_id: int
    product_id: int
    bus: int | None
    address: int | None
    serial_number: str | None
    label: str

    @property
    def selector_value(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}:{self.bus}:{self.address}"


def discover_usb_devices() -> list[UsbDeviceInfo]:
    import usb.core

    devices = []
    for device in usb.core.find(find_all=True) or ():
        manufacturer = _usb_string(device, "manufacturer")
        product = _usb_string(device, "product")
        if not _is_supported_usb_device(device, product):
            continue
        serial_number = _usb_string(device, "serial_number")
        description = " ".join(part for part in (manufacturer, product) if part) or "USB device"
        location = f"bus {device.bus}, address {device.address}"
        devices.append(
            UsbDeviceInfo(
                vendor_id=device.idVendor,
                product_id=device.idProduct,
                bus=device.bus,
                address=device.address,
                serial_number=serial_number,
                label=f"{description} ({device.idVendor:04x}:{device.idProduct:04x}, {location})",
            )
        )
    return devices


def _usb_string(device, attribute: str) -> str | None:
    try:
        return getattr(device, attribute, None)
    except Exception:
        return None


def _is_supported_usb_device(device, product: str | None = None) -> bool:
    product = product if product is not None else _usb_string(device, "product")
    return (
        device.idVendor == EPSON_VENDOR_ID
        and device.idProduct == LABELWORKS_PRODUCT_ID
        and product is not None
        and SUPPORTED_USB_MODEL in product.upper()
    )


class PrinterTransport(ABC):
    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read(self, size: int, timeout_s: float) -> bytes: ...

    def read_status(self, timeout_s: float = 10) -> protocol.Status:
        deadline = time.monotonic() + timeout_s
        pending = bytearray()
        while time.monotonic() < deadline:
            pending.extend(self.read(512, min(1, deadline - time.monotonic())))
            frame = protocol.find_status_frame(pending)
            if frame is not None:
                return protocol.parse_status(frame)
            if len(pending) > 4096:
                del pending[:-4096]
        raise TimeoutError("timed out waiting for printer status")

    def status(self) -> protocol.Status:
        return self._query_status()

    def _query_status(self, timeout_s: float = 10) -> protocol.Status:
        self.write(protocol.request_status())
        return self.read_status(timeout_s)

    def _status_is_complete(self, status: protocol.Status, require_print_end: bool) -> bool:
        return status.status_code == 0x05 if require_print_end else status.ready_for_print

    def print_image(
        self,
        image,
        cut: protocol.CutMode,
        density: int,
        margin_dots: int,
        tape_width_mm: float,
    ) -> protocol.Status:
        preflight = self.status()
        if preflight.error_code:
            raise RuntimeError(f"printer error before printing: {preflight.error}")
        if not preflight.ready_for_print:
            raise RuntimeError(f"printer is not ready: {preflight.status}")
        if preflight.tape_width_mm is None:
            raise RuntimeError("unable to determine installed tape width")
        if preflight.tape_width_mm != tape_width_mm:
            raise RuntimeError(
                f"installed tape is {preflight.tape_width_mm} mm, but the label requests {tape_width_mm:g} mm"
            )
        self.write(protocol.reset_printer())
        time.sleep(0.5)
        self.write(protocol.build_print_stream(image, cut, density, margin_dots))
        status = self.wait_until_ready(require_print_end=True)
        self.write(protocol.reset_status_request())
        time.sleep(1)
        return status

    def wait_until_ready(self, timeout_s: float = 60, require_print_end: bool = False) -> protocol.Status:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self._query_status(min(5, max(1, deadline - time.monotonic())))
            if status.error_code or self._status_is_complete(status, require_print_end):
                return status
            time.sleep(STATUS_POLL_INTERVAL_S)
        raise TimeoutError("timed out waiting for printer to finish")


class UsbTransport(PrinterTransport):
    def __init__(
        self,
        vendor_id: int,
        product_id: int,
        interface: int = 0,
        bus: int | None = None,
        address: int | None = None,
        serial_number: str | None = None,
    ):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.interface = interface
        self.bus = bus
        self.address = address
        self.serial_number = serial_number
        self.device = self.out_endpoint = self.in_endpoint = None

    def open(self) -> None:
        import usb.core
        import usb.util

        def matches(candidate) -> bool:
            if self.serial_number:
                return _usb_string(candidate, "serial_number") == self.serial_number
            if self.bus is not None and self.address is not None:
                return candidate.bus == self.bus and candidate.address == self.address
            return True

        device = usb.core.find(idVendor=self.vendor_id, idProduct=self.product_id, custom_match=matches)
        if device is None:
            raise RuntimeError(f"USB Epson printer not found ({self.vendor_id:04x}:{self.product_id:04x})")
        if not _is_supported_usb_device(device):
            raise RuntimeError(f"unsupported USB printer; expected Epson {SUPPORTED_USB_MODEL}")
        device.reset()
        try:
            device.set_configuration()
        except usb.core.USBError:
            pass
        try:
            if device.is_kernel_driver_active(self.interface):
                device.detach_kernel_driver(self.interface)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(device, self.interface)
        interface = usb.util.find_descriptor(device.get_active_configuration(), bInterfaceNumber=self.interface)
        if interface is None or interface.bInterfaceClass != 0x07:
            usb.util.release_interface(device, self.interface)
            raise RuntimeError("selected USB interface is not a printer interface")
        self.out_endpoint = usb.util.find_descriptor(interface, custom_match=lambda endpoint: usb.util.endpoint_direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.in_endpoint = usb.util.find_descriptor(interface, custom_match=lambda endpoint: usb.util.endpoint_direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_IN)
        if self.out_endpoint is None or self.in_endpoint is None:
            raise RuntimeError("USB bulk endpoints not found")
        device.clear_halt(self.out_endpoint.bEndpointAddress)
        device.clear_halt(self.in_endpoint.bEndpointAddress)
        self.device = device

    def _query_status(self, timeout_s: float = 10) -> protocol.Status:
        if self.device is None:
            raise RuntimeError("USB transport is closed")
        response = self.device.ctrl_transfer(
            0xC1,
            0x01,
            0x0000,
            0x0000,
            64,
            timeout=max(1, round(timeout_s * 1000)),
        )
        return protocol.parse_usb_status(bytes(response))

    def _status_is_complete(self, status: protocol.Status, require_print_end: bool) -> bool:
        # USB activity has no distinct print-end value; unlike queued Q replies,
        # this control response reflects printer state at the time of the poll.
        return status.status_code == 0x00

    def close(self) -> None:
        if self.device is not None:
            import usb.util

            try:
                usb.util.release_interface(self.device, self.interface)
            finally:
                usb.util.dispose_resources(self.device)
        self.device = self.out_endpoint = self.in_endpoint = None

    def write(self, data: bytes) -> None:
        if self.out_endpoint is None:
            raise RuntimeError("USB transport is closed")
        for chunk in protocol.stream_chunks(data):
            written = self.out_endpoint.write(chunk, timeout=5000)
            if written != len(chunk):
                raise RuntimeError(f"short USB write ({written} of {len(chunk)} bytes)")

    def read(self, size: int, timeout_s: float) -> bytes:
        if self.in_endpoint is None:
            raise RuntimeError("USB transport is closed")
        import usb.core

        try:
            return bytes(self.in_endpoint.read(size, timeout=max(1, round(timeout_s * 1000))))
        except usb.core.USBTimeoutError:
            return b""


class BluetoothSerialTransport(PrinterTransport):
    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self.serial = None

    def open(self) -> None:
        import serial

        self.serial = serial.Serial(self.port, self.baudrate, timeout=1, write_timeout=5)

    def close(self) -> None:
        if self.serial is not None:
            self.serial.close()
        self.serial = None

    def write(self, data: bytes) -> None:
        if self.serial is None:
            raise RuntimeError("Bluetooth serial transport is closed")
        if self.serial.write(data) != len(data):
            raise RuntimeError("short Bluetooth serial write")
        self.serial.flush()

    def read(self, size: int, timeout_s: float) -> bytes:
        if self.serial is None:
            raise RuntimeError("Bluetooth serial transport is closed")
        self.serial.timeout = timeout_s
        return self.serial.read(size)
