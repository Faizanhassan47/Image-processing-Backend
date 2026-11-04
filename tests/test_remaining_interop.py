import json

import pytest
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from services.barcode_label import encode_code128, render_code128_png, render_datamatrix_png
from services.dicomweb_client import retrieve_instance, search_studies, store_instance
from services.hl7_mllp import send_mllp, serve, tls_context


def test_code128_label_is_a_png():
    symbols = encode_code128("ACC-DEMO03")
    assert symbols[0] == 104 and symbols[-1] == 106
    png = render_code128_png("ACC-DEMO03")
    assert png.startswith(b"\x89PNG")
    matrix = render_datamatrix_png("ACC-DEMO03")
    assert matrix.startswith(b"\x89PNG")


def test_tls_mllp_round_trip(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vista-test")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    key.write_bytes(private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    server_context = tls_context(True, str(cert), str(key))
    client_context = tls_context(False)
    stop = threading.Event()

    def handler(message: str) -> str:
        return "MSH|^~\\&|DP|LAB|SIM|HOSP|20260101010101||ACK|1|P|2.5.1\rMSA|AA|TLS1"

    thread = threading.Thread(target=serve, args=(handler, "127.0.0.1", 25771, stop), kwargs={"ssl_context": server_context}, daemon=True)
    thread.start()
    time.sleep(0.3)
    try:
        ack = send_mllp("127.0.0.1", 25771, "MSH|^~\\&|A|B|C|D|20260101010101||ADT^A04|TLS1|P|2.5.1", timeout=5, ssl_context=client_context)
        assert "MSA|AA|TLS1" in ack
    finally:
        stop.set()
        thread.join(timeout=2)


def test_synthetic_dicom_wsi_opens_and_returns_a_patch(tmp_path):
    openslide = pytest.importorskip("openslide")
    from services.dicom_wsi_sample import write_synthetic_wsi
    from services.slide_patches import read_rgb_patch

    path = write_synthetic_wsi(tmp_path)
    image, mpp = read_rgb_patch(path, 0, 0, 64, 64, openslide)
    assert image.size == (64, 64)
    assert image.getpixel((10, 10)) == (180, 0, 0)
    assert mpp == pytest.approx(1.953125)


def test_dicomweb_client_against_local_archive(tmp_path, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            body = json.dumps([{"0020000D": {"Value": ["1.2.3"]}}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/dicom+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DICOMWEB_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        studies = search_studies("MRN-DEMO03")
        assert studies[0]["0020000D"]["Value"][0] == "1.2.3"
        source = tmp_path / "slide.dcm"
        source.write_bytes(b"DICM")
        store_instance(source)
        destination = tmp_path / "out.dcm"
        retrieve_instance("1.2.3", "1.2.4", "1.2.5", destination)
        assert destination.read_bytes()
    finally:
        server.shutdown()
