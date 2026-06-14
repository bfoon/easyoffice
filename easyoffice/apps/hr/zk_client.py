"""
Thin wrapper around the `pyzk` library for ZKTeco terminals.

Install on the server:  pip install pyzk

This module isolates all hardware/SDK concerns. Nothing else in the codebase
should import `zk` directly — everything goes through ZKClient so the rest of
the app stays testable and the dependency stays in one place.
"""
import logging

logger = logging.getLogger(__name__)


class ZKConnectionError(Exception):
    """Raised when we cannot reach or talk to a terminal."""


class ZKClient:
    """
    Context-managed connection to a single ZKDevice.

    Usage:
        with ZKClient(device) as client:
            for punch in client.iter_attendance():
                ...
    """

    def __init__(self, device):
        self.device = device
        self._zk = None
        self._conn = None

    def __enter__(self):
        try:
            # Imported lazily so the app boots even if pyzk isn't installed yet
            # (e.g. on a dev machine with no terminal access).
            from zk import ZK
        except ImportError as exc:  # pragma: no cover
            raise ZKConnectionError(
                "The 'pyzk' package is not installed. Run: pip install pyzk"
            ) from exc

        self._zk = ZK(
            self.device.ip_address,
            port=self.device.port,
            timeout=self.device.timeout,
            password=self.device.comm_password or 0,
            force_udp=self.device.force_udp,
            ommit_ping=True,
        )
        try:
            self._conn = self._zk.connect()
        except Exception as exc:
            raise ZKConnectionError(
                f'Could not connect to {self.device.name} '
                f'({self.device.ip_address}:{self.device.port}): {exc}'
            ) from exc
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn is not None:
            try:
                self._conn.enable_device()
            except Exception:
                pass
            try:
                self._conn.disconnect()
            except Exception:
                pass
        self._conn = None
        return False

    # -- device info -------------------------------------------------------

    def device_info(self):
        """Return a dict of basic device metadata (best-effort)."""
        info = {}
        try:
            info['serial_number'] = self._conn.get_serialnumber()
            info['firmware'] = self._conn.get_firmware_version()
            info['device_name'] = self._conn.get_device_name()
            info['platform'] = self._conn.get_platform()
        except Exception as exc:  # pragma: no cover
            logger.warning('Could not read device info for %s: %s', self.device, exc)
        return info

    # -- enrolled users ----------------------------------------------------

    def iter_users(self):
        """
        Yield dicts of enrolled users so HR can map enroll IDs to staff.
        Returns: {'user_id': str, 'name': str, 'privilege': int}
        """
        try:
            for u in self._conn.get_users():
                yield {
                    'user_id': str(u.user_id),
                    'name': (u.name or '').strip(),
                    'privilege': getattr(u, 'privilege', 0),
                }
        except Exception as exc:
            raise ZKConnectionError(f'Failed to read users from {self.device}: {exc}') from exc

    # -- punch logs --------------------------------------------------------

    def iter_attendance(self):
        """
        Yield raw punch records from the device buffer.
        Returns dicts: {'user_id', 'timestamp', 'status', 'punch'}
        `timestamp` is a naive datetime in the device's local time.
        """
        try:
            # Disabling the device during a bulk read prevents the buffer from
            # shifting mid-read on busy terminals.
            self._conn.disable_device()
            for att in self._conn.get_attendance():
                yield {
                    'user_id': str(att.user_id),
                    'timestamp': att.timestamp,
                    'status': getattr(att, 'status', 0),
                    'punch': getattr(att, 'punch', 0),
                }
        except Exception as exc:
            raise ZKConnectionError(f'Failed to read attendance from {self.device}: {exc}') from exc
        finally:
            try:
                self._conn.enable_device()
            except Exception:
                pass

    def clear_attendance(self):
        """Wipe the device's punch buffer. Use only when this server is the sole reader."""
        try:
            self._conn.clear_attendance()
        except Exception as exc:
            raise ZKConnectionError(f'Failed to clear attendance on {self.device}: {exc}') from exc

    def test_connection(self):
        """Return device info; raises ZKConnectionError on failure. Used by the 'Test' button."""
        return self.device_info()
