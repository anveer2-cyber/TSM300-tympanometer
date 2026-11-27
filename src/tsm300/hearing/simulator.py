"""A make-believe Resonance audiometer, for trying the program without the device.

It answers the way docs/hearing/통신_절차.md says the device does: the identification with its serial
number (``KI``) or an ack, "get all" with every patient (``KA``) and test (``KT``) in its
memory followed by an ack. Variants of that behaviour can be switched on to make sure the
program copes with what a real device might do differently.

    python -m tsm300.hearing.simulator --port 50555        # serve it on TCP for another program
"""

from __future__ import annotations

import argparse
import socket
import threading
from dataclasses import dataclass, field

from . import protocol as p


@dataclass
class Behaviour:
    answer_identification: str = "KI"
    """'KI' (serial number), 'KI+ACK' or 'ACK' (ack with the protocol version only)."""
    ack_at_end: bool = True
    chunk: int = 64
    """Bytes handed over per read, like a USB full-speed packet."""
    protocol_version: int = 3
    noise_before: bytes = b""
    unasked_test: bool = False
    """Sends the newest test as soon as it is identified, as old firmware did."""
    ignore_length_field: bool = True
    """False: refuses commands whose length field is not 0x0A or 0x10."""


@dataclass
class Memory:
    serial: str = "R37A21C000777"
    groups: list[tuple[bytes, list[bytes]]] = field(default_factory=list)
    """(KA packet, [KT packets]) in the device's order."""

    def add(self, number: int, first: str, surname: str, birth: str, sex: str, tests: list[bytes],
            pc_number: int = 0) -> None:
        data = f"{number};{pc_number};{first};{surname};{birth};{sex};;;".encode("utf-8")
        self.groups.append((p.frame(p.KIND_PATIENT, data), list(tests)))


class SimulatedAudiometer:
    """Plays the device's side of the link: ``write`` takes the PC's bytes, ``read`` gives answers."""

    name = "모의 청력검사기"

    def __init__(self, memory: Memory, behaviour: Behaviour | None = None) -> None:
        self.memory = memory
        self.behaviour = behaviour or Behaviour()
        self.received: list[bytes] = []
        self.refused: list[bytes] = []
        self._outgoing = bytearray(self.behaviour.noise_before)
        self._incoming = bytearray()
        self._identified = False
        self.closed = False

    # --- the link, seen from the PC ------------------------------------------------------

    def write(self, data: bytes) -> None:
        self._incoming += data
        while len(self._incoming) >= 16:
            command = bytes(self._incoming[:16])
            del self._incoming[:16]
            self._handle(command)

    def read(self) -> bytes:
        chunk = bytes(self._outgoing[:self.behaviour.chunk])
        del self._outgoing[:len(chunk)]
        return chunk

    def close(self) -> None:
        self.closed = True

    # --- the device ----------------------------------------------------------------------

    def _ack(self, value: int = 0) -> bytes:
        body = bytes([p.ACK, value]).ljust(10, b"\x00")
        return bytes([p.START, p.KIND_COMMAND, 0, 10]) + body + bytes([p.xor(body), p.END])

    def _handle(self, command: bytes) -> None:
        self.received.append(command)
        if command[:2] != b"KC" or command[-1] != p.END or p.xor(command[4:14]) != command[14]:
            self.refused.append(command)
            self._outgoing += p.frame(p.KIND_COMMAND, bytes([p.NACK]).ljust(10, b"\x00"))
            return
        if not self.behaviour.ignore_length_field and command[3] not in (0x0A, 0x10):
            self.refused.append(command)
            return
        number = command[4]
        if number == p.SYNC:
            answer = self.behaviour.answer_identification
            if "KI" in answer and not self._identified:
                self._outgoing += p.frame(p.KIND_IDENTIFICATION, self.memory.serial.encode("ascii") + b"\x00")
            if "ACK" in answer or self._identified:
                self._outgoing += self._ack(self.behaviour.protocol_version)
            if self.behaviour.unasked_test and not self._identified and self.memory.groups:
                self._outgoing += self.memory.groups[-1][1][-1]
            self._identified = True
        elif number == p.GET_ALL:
            for patient, tests in self.memory.groups:
                self._outgoing += patient
                for test in tests:
                    self._outgoing += test
            if self.behaviour.ack_at_end:
                self._outgoing += self._ack()
        elif number == p.PING:
            self._outgoing += self._ack()
        elif number in (p.ACK, p.NACK, p.DEVICE_INFO):
            pass
        else:
            self.refused.append(command)


def serve(device: SimulatedAudiometer, port: int) -> None:
    """Offers the simulated device on a TCP port, one connection at a time."""
    server = socket.create_server(("127.0.0.1", port))
    print(f"모의 청력검사기 {device.memory.serial} — 127.0.0.1:{port}")
    while True:
        connection, _ = server.accept()
        connection.settimeout(0.05)

        def pump() -> None:
            while True:
                out = device.read()
                if out:
                    connection.sendall(out)
                try:
                    data = connection.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    return
                device.write(data)

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        thread.join()
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=50555)
    args = parser.parse_args()
    from .samples import demo_memory
    serve(SimulatedAudiometer(demo_memory()), args.port)


if __name__ == "__main__":
    main()
