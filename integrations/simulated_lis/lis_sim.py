"""Send a synthetic patient registration and laboratory order over MLLP.

This process is not part of the VISTA API. Start the listener first:

    python -m services.hl7_mllp

Then:

    python -m integrations.simulated_lis.lis_sim
"""

from __future__ import annotations

import argparse
import threading
from datetime import datetime, timezone
from uuid import uuid4

from services.hl7_engine import generate_hl7_ack, parse_raw_hl7
from services.hl7_mllp import send_mllp, serve


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def build_messages(mrn: str, order_id: str, accession: str) -> tuple[str, str]:
    now = _stamp()
    adt = "\r".join([
        f"MSH|^~\\&|SIM_LIS|SIM_HOSP|DP_PATHOLOGY|LAB_PATH|{now}||ADT^A04|MSG-{uuid4().hex[:10]}|P|2.5.1",
        f"EVN|A04|{now}",
        f"PID|1||{mrn}^^^SIM_HOSP||DOE^JANE^Q||19800101|F|||1 TEST WAY^^CITY^^00000||5550100",
        "PV1|1|O|PATH",
    ])
    orm = "\r".join([
        f"MSH|^~\\&|SIM_LIS|SIM_HOSP|DP_PATHOLOGY|LAB_PATH|{now}||ORM^O01|MSG-{uuid4().hex[:10]}|P|2.5.1",
        f"PID|1||{mrn}^^^SIM_HOSP||DOE^JANE^Q||19800101|F",
        f"ORC|NW|{order_id}",
        f"OBR|1|{order_id}|{accession}|88305^SURGICAL PATHOLOGY^CPT",
    ])
    return adt, orm


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulated hospital LIS over MLLP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2575)
    parser.add_argument("--mrn", default=f"MRN-{uuid4().hex[:8].upper()}")
    parser.add_argument("--accession", default=f"ACC-{uuid4().hex[:8].upper()}")
    parser.add_argument("--result-port", type=int, default=2576)
    args = parser.parse_args()
    order_id = f"ORD-{args.accession}"
    adt, orm = build_messages(args.mrn, order_id, args.accession)

    def accept_result(message: str) -> str:
        parsed = parse_raw_hl7(message)
        print("Received result")
        print(message.replace("\r", "\n"))
        return generate_hl7_ack(parsed, accepted=True)

    stop = threading.Event()
    listener = threading.Thread(target=serve, args=(accept_result, "127.0.0.1", args.result_port, stop), daemon=True)
    listener.start()
    print(f"Patient {args.mrn}")
    print(send_mllp(args.host, args.port, adt))
    print(f"Order {args.accession}")
    print(send_mllp(args.host, args.port, orm))
    print(f"Waiting for ORU on 127.0.0.1:{args.result_port}. Finalize the report in VISTA. Press Ctrl+C to stop.")
    try:
        listener.join()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
