"""A minimal 8N1 UART model: what a receiver at one baud rate reads from a sender at another."""

from __future__ import annotations


def frame_bits(payload: bytes) -> list[int]:
    bits: list[int] = []
    for byte in payload:
        bits.append(0)
        bits.extend((byte >> i) & 1 for i in range(8))
        bits.append(1)
    return bits


def receive(payload: bytes, tx_baud: int, rx_baud: int) -> bytes:
    bits = frame_bits(payload)
    tx_bit = 1.0 / tx_baud
    rx_bit = 1.0 / rx_baud
    skew = 0.01 * tx_bit

    def level(t: float) -> int:
        index = int((t + skew) // tx_bit)
        return bits[index] if 0 <= index < len(bits) else 1

    falling_edges = [
        i * tx_bit for i, bit in enumerate(bits)
        if bit == 0 and (i == 0 or bits[i - 1] == 1)
    ]
    received = bytearray()
    ready_at = 0.0
    for edge in falling_edges:
        if edge < ready_at or level(edge + 0.5 * rx_bit) != 0:
            continue
        value = 0
        for i in range(8):
            value |= level(edge + (1.5 + i) * rx_bit) << i
        received.append(value)
        ready_at = edge + 9.5 * rx_bit
    return bytes(received)
