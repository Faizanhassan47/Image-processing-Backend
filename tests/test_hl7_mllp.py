import threading
import time

from integrations.simulated_lis.lis_sim import build_messages
from services.hl7_engine import build_oru_r01, parse_raw_hl7
from services.hl7_mllp import encode_mllp, extract_messages, send_mllp, serve


def test_frame_round_trip():
    payload = "MSH|^~\\&|A|B|C|D|20260101010101||ADT^A04|1|P|2.5.1\rPID|1||MRN1"
    buffer = bytearray(b"noise" + encode_mllp(payload) + encode_mllp("MSH|^~\\&|A|B|C|D|20260101010101||ACK|2|P|2.5.1\rMSA|AA|1"))
    messages = extract_messages(buffer)
    assert messages[0].startswith("MSH")
    assert "MSA|AA|1" in messages[1]
    assert buffer == bytearray()


def test_listener_acknowledges_simulator_order():
    adt, orm = build_messages("MRN-SIM-1", "ORD-1", "ACC-1")

    def handler(message: str) -> str:
        parsed = parse_raw_hl7(message)
        return f"MSH|^~\\&|DP|LAB|SIM|HOSP|20260101010101||ACK|ACK1|P|2.5.1\rMSA|AA|{parsed['message_control_id']}"

    port = 25751
    stop = threading.Event()
    thread = threading.Thread(target=serve, args=(handler, "127.0.0.1", port, stop), daemon=True)
    thread.start()
    time.sleep(0.3)
    try:
        ack_adt = send_mllp("127.0.0.1", port, adt, timeout=5)
        ack_orm = send_mllp("127.0.0.1", port, orm, timeout=5)
        assert "MSA|AA|" in ack_adt
        assert "MSA|AA|" in ack_orm
        result_port = 25761
        received = {}

        def accept_result(message: str) -> str:
            received["message"] = message
            return "MSH|^~\\&|SIM|HOSP|DP|LAB|20260101010101||ACK|R1|P|2.5.1\rMSA|AA|ORU-1"

        result_stop = threading.Event()
        result_thread = threading.Thread(target=serve, args=(accept_result, "127.0.0.1", result_port, result_stop), daemon=True)
        result_thread.start()
        time.sleep(0.2)
        oru = build_oru_r01("MRN-SIM-1", "ORD-1", "ACC-1", "Benign tissue", "ORU-1")
        result_ack = send_mllp("127.0.0.1", result_port, oru, timeout=5)
        assert "DIAGNOSIS" in received["message"]
        assert "MSA|AA|ORU-1" in result_ack
        result_stop.set()
        result_thread.join(timeout=3)
    finally:
        stop.set()
        thread.join(timeout=3)
