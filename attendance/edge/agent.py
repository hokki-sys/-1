#!/usr/bin/env python3
"""매장 단말 에이전트 (설계 D1·D2).

이 프로그램은 **판정하지 않습니다.** 카드가 찍힌 사실만 로컬 큐에 적고 즉시
화면에 응답한 뒤, 네트워크가 되면 서버로 올립니다. 출근인지 퇴근인지는
서버가 정합니다 — 오프라인이면 직전 기록을 알 방법이 없기 때문입니다.

인터넷이 끊겨도 다음은 그대로 동작합니다:
  카드 읽기 -> 로컬 큐에 적재 -> 화면에 "기록되었습니다"

의존성은 표준 라이브러리뿐입니다. 시리얼 리더기를 쓸 때만 pyserial 이 필요합니다.

    python3 agent.py --config agent.ini
    python3 agent.py --config agent.ini --stdin    # 리더기 없이 테스트
"""
from __future__ import annotations

import argparse
import configparser
import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = "agent.ini"
QUEUE_FILE = "queue.db"
BATCH_SIZE = 50
SYNC_INTERVAL = 5          # 초. 큐가 비어 있으면 하트비트만 보냅니다.
BACKOFF_MAX = 300
CARD_MIN_LEN, CARD_MAX_LEN = 6, 24
#: 사람이 손으로 친 숫자를 카드로 오인하지 않기 위한 입력 속도 제한.
#: 리더기는 한 자리를 50ms 안에 보냅니다.
MAX_KEY_GAP_MS = 120


class Queue:
    """오프라인 큐. 이게 이 프로그램의 핵심입니다."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS outbox(
                 client_event_id TEXT PRIMARY KEY,
                 card_uid TEXT NOT NULL,
                 tapped_at TEXT NOT NULL,
                 created_at TEXT NOT NULL)"""
        )
        self.conn.commit()
        self.lock = threading.Lock()

    def add(self, card_uid: str, tapped_at: datetime) -> str:
        event_id = uuid.uuid4().hex
        with self.lock:
            self.conn.execute(
                "INSERT INTO outbox VALUES (?,?,?,?)",
                (event_id, card_uid, tapped_at.isoformat(),
                 datetime.now(timezone.utc).isoformat()),
            )
            self.conn.commit()
        return event_id

    def peek(self, limit: int = BATCH_SIZE) -> list[tuple[str, str, str]]:
        with self.lock:
            return self.conn.execute(
                "SELECT client_event_id, card_uid, tapped_at FROM outbox "
                "ORDER BY tapped_at LIMIT ?", (limit,)
            ).fetchall()

    def drop(self, ids: list[str]) -> None:
        if not ids:
            return
        with self.lock:
            self.conn.executemany(
                "DELETE FROM outbox WHERE client_event_id=?", [(i,) for i in ids]
            )
            self.conn.commit()

    def depth(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


class Client:
    def __init__(self, base_url: str, token: str, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def send(self, rows: list[tuple[str, str, str]], depth: int) -> dict:
        return self._post("/api/punches", {
            "punches": [
                {"client_event_id": i, "card_uid": c, "tapped_at": t} for i, c, t in rows
            ],
            "device_time": datetime.now(timezone.utc).isoformat(),
            "queue_depth": depth,
        })


class Agent:
    def __init__(self, cfg: configparser.ConfigParser, base_dir: Path):
        self.base_url = cfg.get("server", "base_url")
        self.token = cfg.get("server", "token")
        self.queue = Queue(base_dir / cfg.get("device", "queue_file", fallback=QUEUE_FILE))
        self.client = Client(self.base_url, self.token)
        self.stop = threading.Event()
        #: 탭이 들어오면 즉시 깨웁니다. 타이머만 믿으면 최대 SYNC_INTERVAL 만큼
        #: 늦게 올라가고, 그동안 관리자 화면에 안 보입니다.
        self.wake = threading.Event()
        #: 타이머와 탭 이벤트가 동시에 sync 를 부르면 같은 큐를 두 번 보냅니다.
        #: 서버가 멱등이라 데이터는 안전하지만, 쓸데없는 왕복을 막습니다.
        self.sync_lock = threading.Lock()
        self.online = False

    # ------------------------------------------------------------ 카드 입력
    def on_card(self, card_uid: str) -> None:
        """찍힌 즉시 로컬에 적고 화면에 응답합니다. 서버를 기다리지 않습니다."""
        tapped_at = datetime.now(timezone.utc)
        self.queue.add(card_uid, tapped_at)
        depth = self.queue.depth()
        state = "" if self.online else "  [오프라인 — 큐에 보관 중]"
        print(f"\n  ● {tapped_at.astimezone():%H:%M:%S}  카드 {card_uid} 기록되었습니다"
              f"{state}", flush=True)
        if depth > 1:
            print(f"    전송 대기 {depth}건", flush=True)
        self.wake.set()

    # ------------------------------------------------------------ 동기화
    def sync_loop(self) -> None:
        backoff = 0.0        # 시작하자마자 한 번 붙어 봅니다 — 연결 상태를 알아야
        while not self.stop.is_set():
            # 탭이 들어오면 타이머를 기다리지 않고 바로 깨어납니다.
            self.wake.wait(timeout=backoff)
            self.wake.clear()
            if self.stop.is_set():
                return
            if self.sync_once() is False:
                backoff = min(BACKOFF_MAX, max(SYNC_INTERVAL, backoff * 2 or SYNC_INTERVAL))
            else:
                backoff = SYNC_INTERVAL

    def sync_once(self, wait: float = 0.0) -> bool:
        """한 번 전송 시도. 성공하면 True.

        `wait` 를 주면 다른 스레드가 보내는 중일 때 그만큼 기다립니다.
        종료 직전 마지막 전송에서만 씁니다 — 평소엔 겹쳐 보낼 이유가 없습니다.
        """
        got = (self.sync_lock.acquire(timeout=wait) if wait > 0
               else self.sync_lock.acquire(blocking=False))
        if not got:
            return True          # 이미 다른 스레드가 보내는 중
        try:
            return self._sync_locked()
        finally:
            self.sync_lock.release()

    def _sync_locked(self) -> bool:
        rows = self.queue.peek()
        depth = self.queue.depth()
        try:
            body = self.client.send(rows, depth)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._log("단말 토큰이 거부되었습니다. agent.ini 를 확인하세요.")
            else:
                self._log(f"서버 오류 {exc.code} — 다시 시도합니다")
            self.online = False
            return False
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if self.online:
                self._log(f"연결이 끊겼습니다 ({exc}). 큐에 계속 쌓습니다.")
            self.online = False
            return False

        if not self.online:
            self._log("서버에 연결되었습니다")
        self.online = True

        # 서버가 받았다고 한 것만 큐에서 지웁니다. 멱등이라 중복 전송은 안전합니다.
        accepted = [
            r["client_event_id"] for r in body.get("results", []) if r.get("accepted")
        ]
        self.queue.drop(accepted)
        for r in body.get("results", []):
            if r.get("accepted") and not r.get("duplicate") and not r.get("known_card"):
                self._log("등록되지 않은 카드입니다 — 관리자 화면에서 직원에 연결하세요")
        if body.get("clock_warning"):
            self._log(body["clock_warning"])
        return True

    def _log(self, message: str) -> None:
        print(f"  [{datetime.now():%H:%M:%S}] {message}", flush=True)

    # ------------------------------------------------------------ 리더기
    def read_serial(self, port: str, baud: int) -> None:
        try:
            import serial  # type: ignore
        except ImportError:
            sys.exit("시리얼 리더기를 쓰려면 pyserial 이 필요합니다: pip install pyserial")
        with serial.Serial(port, baud, timeout=1) as ser:
            self._log(f"시리얼 리더기 감시 중: {port} @ {baud}")
            buf = b""
            while not self.stop.is_set():
                chunk = ser.read(64)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf or b"\r" in buf:
                    line, _, buf = buf.replace(b"\r", b"\n").partition(b"\n")
                    card = line.decode(errors="ignore").strip()
                    if self._valid(card):
                        self.on_card(card)

    def read_stdin(self) -> None:
        """리더기 없이 테스트할 때. 카드 번호를 치고 엔터."""
        self._log("표준입력에서 카드 번호를 기다립니다 (Ctrl-D 로 종료)")
        for line in sys.stdin:
            card = line.strip()
            if self._valid(card):
                self.on_card(card)
            elif card:
                print(f"  무시: {card!r} (숫자 {CARD_MIN_LEN}~{CARD_MAX_LEN}자리가 아닙니다)")

    @staticmethod
    def _valid(card: str) -> bool:
        return card.isdigit() and CARD_MIN_LEN <= len(card) <= CARD_MAX_LEN


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--stdin", action="store_true", help="리더기 없이 표준입력으로 테스트")
    args = ap.parse_args()

    path = Path(args.config)
    if not path.exists():
        sys.exit(f"설정 파일이 없습니다: {path}\nagent.ini.example 을 복사해 채우세요.")
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")

    agent = Agent(cfg, path.resolve().parent)
    depth = agent.queue.depth()
    print(f"\n  출퇴근 단말  ->  {agent.base_url}")
    if depth:
        print(f"  전송 대기 {depth}건이 남아 있습니다. 연결되면 자동으로 올라갑니다.")
    print()

    threading.Thread(target=agent.sync_loop, daemon=True).start()
    try:
        if args.stdin:
            agent.read_stdin()
        else:
            agent.read_serial(
                cfg.get("reader", "port", fallback="/dev/ttyUSB0"),
                cfg.getint("reader", "baudrate", fallback=9600),
            )
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop.set()
        agent.wake.set()
        # 나가기 전에 한 번 더 밀어 봅니다. 남으면 다음 실행 때 올라갑니다.
        try:
            agent.sync_once(wait=5.0)
        except Exception as exc:      # 조용히 삼키면 왜 안 올라갔는지 알 수 없습니다
            print(f"  마지막 전송 실패: {exc}")
        left = agent.queue.depth()
        if left:
            print(f"\n  종료합니다. 전송 대기 {left}건은 큐에 남아 있습니다.")
        else:
            print("\n  종료합니다. 전송할 기록이 모두 올라갔습니다.")


if __name__ == "__main__":
    main()
