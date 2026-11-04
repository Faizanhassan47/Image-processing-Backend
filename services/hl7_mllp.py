"""MLLP framing and a supervised HL7 v2 listener.

Run this as its own process. Do not start it from a Uvicorn worker.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import threading
from typing import Callable

logger = logging.getLogger("pathology.hl7.mllp")

MLLP_START = b"\x0b"
MLLP_END = b"\x1c\x0d"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024


def encode_mllp(message: str) -> bytes:
    payload = message.replace("\n", "\r").encode("utf-8")
    return MLLP_START + payload + MLLP_END


def extract_messages(buffer: bytearray, max_bytes: int = DEFAULT_MAX_BYTES) -> list[str]:
    messages = []
    while True:
        start = buffer.find(MLLP_START)
        if start < 0:
            if len(buffer) > max_bytes:
                raise ValueError("MLLP frame exceeded the size limit before a start byte")
            buffer.clear()
            break
        if start:
            del buffer[:start]
        end = buffer.find(MLLP_END, 1)
        if end < 0:
            if len(buffer) > max_bytes:
                raise ValueError("MLLP frame exceeded the size limit")
            break
        payload = bytes(buffer[1:end]).decode("utf-8", errors="replace").strip("\r")
        del buffer[: end + len(MLLP_END)]
        if payload:
            messages.append(payload)
    return messages


def tls_context(server_side: bool, certfile: str | None = None, keyfile: str | None = None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server_side else ssl.PROTOCOL_TLS_CLIENT)
    if server_side:
        context.load_cert_chain(certfile, keyfile)
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def send_mllp(host: str, port: int, message: str, timeout: float = 15.0, ssl_context: ssl.SSLContext | None = None) -> str:
    connection = socket.create_connection((host, port), timeout=timeout)
    if ssl_context is not None:
        connection = ssl_context.wrap_socket(connection, server_hostname=host)
    with connection:
        connection.settimeout(timeout)
        connection.sendall(encode_mllp(message))
        buffer = bytearray()
        while True:
            chunk = connection.recv(8192)
            if not chunk:
                break
            buffer.extend(chunk)
            found = extract_messages(buffer)
            if found:
                return found[0]
    raise TimeoutError("MLLP peer closed the connection before an acknowledgement")


def serve(handler: Callable[[str], str], host: str, port: int, stop_event: threading.Event | None = None, ssl_context: ssl.SSLContext | None = None) -> None:
    """Accept one connection at a time and answer each framed message."""
    stop_event = stop_event or threading.Event()
    timeout = float(os.getenv("HL7_MLLP_TIMEOUT_SECONDS", "30"))
    max_bytes = int(os.getenv("HL7_MLLP_MAX_BYTES", str(DEFAULT_MAX_BYTES)))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(8)
    server.settimeout(1.0)
    logger.info("HL7 MLLP listening on %s:%s", host, port)
    try:
        while not stop_event.is_set():
            try:
                connection, address = server.accept()
            except socket.timeout:
                continue
            logger.info("HL7 connection from %s", address)
            try:
                if ssl_context is not None:
                    connection = ssl_context.wrap_socket(connection, server_side=True)
                connection.settimeout(timeout)
                buffer = bytearray()
                while not stop_event.is_set():
                    try:
                        chunk = connection.recv(8192)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    for message in extract_messages(buffer, max_bytes):
                        reply = handler(message)
                        connection.sendall(encode_mllp(reply))
            except Exception:
                logger.exception("HL7 connection failed")
            finally:
                connection.close()
    finally:
        server.close()


def handle_inbound(raw_payload: str) -> str:
    """Validate, commit clinical records, then return an ACK or NACK."""
    from services.hl7_apply import apply_inbound_message

    return apply_inbound_message(raw_payload)


def main() -> None:
    host = os.getenv("HL7_MLLP_HOST", "127.0.0.1")
    port = int(os.getenv("HL7_MLLP_PORT", "2575"))
    certfile = os.getenv("HL7_MLLP_CERT")
    keyfile = os.getenv("HL7_MLLP_KEY")
    context = tls_context(True, certfile, keyfile) if certfile and keyfile else None
    serve(handle_inbound, host, port, ssl_context=context)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
