"""
로블록스 게임 수치 추적기 (공식 공개 API만 사용, 파이썬 기본 기능만 사용)

공식 API는 "지금 이 순간" 값만 준다. 그래서 일정 간격으로 찍어서 쌓아 두면
그게 곧 우리만의 정확한 히스토리가 된다.

사용법 (자세한 건 README.md):
    python tracker.py              # 계속 실행 (기본 10분 간격)
    python tracker.py --once       # 한 번만 찍고 종료
    python tracker.py --interval 5 # 5분 간격
    python tracker.py add <플레이스 주소 또는 ID> [이름]
    python tracker.py list         # 감시 목록 보기
    python tracker.py summary      # 최근 24시간 요약 (클라우드 데이터를 받아서)
    python tracker.py summary --local   # 이 PC에서 직접 찍은 데이터로 요약
    python tracker.py pull         # 클라우드(GitHub) 데이터만 내려받기

클라우드 실행: GitHub Actions가 10분마다 `tracker.py --once`를 돌리고
data/snapshots/ 를 저장소에 커밋한다 (.github/workflows/snapshot.yml).
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 설정
DEFAULT_INTERVAL_MIN = 10          # 몇 분마다 찍을지
BATCH_SIZE = 50                    # 한 요청에 묶을 게임 수 (API 한도 이하)
REQUEST_GAP_SEC = 1.0              # 요청 사이 쉬는 시간 (레이트 리밋 배려)
MAX_RETRIES = 5

# 클라우드(GitHub Actions)로 쌓는 저장소. "아이디/저장소이름" 형식. 비우면 클라우드 기능 끔.
CLOUD_REPO = "JSwon76/roblox-market-tracker"
CLOUD_BRANCH = "main"

GAMES_BASE = os.environ.get("RBX_GAMES_BASE", "https://games.roblox.com")
APIS_BASE = os.environ.get("RBX_APIS_BASE", "https://apis.roblox.com")

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.csv")
DATA_DIR = os.path.join(HERE, "data")
# 한 파일이 너무 커지지 않게 달(UTC 기준)마다 파일을 나눈다. 예: data/snapshots/2026-11.csv
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
CLOUD_DIR = os.path.join(DATA_DIR, "cloud")   # 클라우드에서 받은 사본 (로컬 기록과 섞지 않음)
LOG_FILE = os.path.join(DATA_DIR, "tracker.log")


def month_file(base_dir, when_utc):
    return os.path.join(base_dir, f"{when_utc:%Y-%m}.csv")


def months_between(start_utc, end_utc):
    """start~end 구간에 걸친 달들 (YYYY-MM 날짜 객체 목록)."""
    y, m = start_utc.year, start_utc.month
    out = []
    while (y, m) <= (end_utc.year, end_utc.month):
        out.append(dt.datetime(y, m, 1, tzinfo=dt.timezone.utc))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out

SNAPSHOT_FIELDS = [
    "time_utc", "time_local", "universe_id", "label",
    "playing", "visits", "favorites", "up_votes", "down_votes", "like_pct",
    "max_players", "game_updated_utc", "name_now",
]
# ---------------------------------------------------------------------


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def http_get_json(url):
    """GET + JSON. 429(너무 많은 요청)와 5xx는 기다렸다가 다시 시도."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "rbx-market-tracker/1.0",
        "Accept": "application/json",
        "Accept-Language": "en-US",   # 게임 이름을 영어 원문으로 받기
    })
    wait = 5
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                retry_after = e.headers.get("Retry-After")
                delay = int(retry_after) if (retry_after or "").isdigit() else wait
                log(f"  HTTP {e.code}, {delay}초 후 재시도 ({attempt}/{MAX_RETRIES})")
                time.sleep(delay)
                wait = min(wait * 2, 120)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log(f"  네트워크 오류: {e}, {wait}초 후 재시도 ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            wait = min(wait * 2, 120)
    raise RuntimeError(f"요청 실패 (재시도 {MAX_RETRIES}회 초과): {url}")


# ---------------------------------------------------------------- 감시 목록
def load_watchlist():
    if not os.path.exists(WATCHLIST):
        return []
    with open(WATCHLIST, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("universe_id") or "").strip()]
    for r in rows:
        r["universe_id"] = r["universe_id"].strip()
    return rows


def save_watchlist(rows):
    with open(WATCHLIST, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["universe_id", "place_id", "label", "memo"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})


def place_to_universe(place_id):
    j = http_get_json(f"{APIS_BASE}/universes/v1/places/{place_id}/universe")
    uid = j.get("universeId")
    if not uid:
        raise ValueError(f"플레이스 {place_id}의 유니버스 ID를 찾지 못했습니다.")
    return str(uid)


def cmd_add(target, label):
    # 로블록스 게임 주소(https://www.roblox.com/games/123456/Name) 또는 숫자 ID
    m = re.search(r"/games/(\d+)", target)
    place_id = m.group(1) if m else target.strip()
    if not place_id.isdigit():
        sys.exit("플레이스 주소나 숫자 ID를 넣어 주세요. 예: https://www.roblox.com/games/126884695634066")
    universe_id = place_to_universe(place_id)
    info = http_get_json(f"{GAMES_BASE}/v1/games?universeIds={universe_id}")["data"][0]
    rows = load_watchlist()
    if any(r["universe_id"] == universe_id for r in rows):
        print(f"이미 목록에 있습니다: {info['name']} (universe {universe_id})")
        return
    rows.append({"universe_id": universe_id, "place_id": place_id,
                 "label": label or info["name"], "memo": ""})
    save_watchlist(rows)
    print(f"추가했습니다: {label or info['name']} (universe {universe_id}, 지금 동접 {info['playing']:,})")


def cmd_list():
    rows = load_watchlist()
    for r in rows:
        print(f"{r['universe_id']:>14}  {r['label']}  {r.get('memo','')}")
    print(f"총 {len(rows)}개")


# ---------------------------------------------------------------- 스냅샷
def take_snapshot():
    rows = load_watchlist()
    if not rows:
        log("감시 목록이 비어 있습니다. watchlist.csv를 확인하세요.")
        return 0
    labels = {r["universe_id"]: r["label"] for r in rows}
    ids = list(labels)

    games, votes = {}, {}
    for i in range(0, len(ids), BATCH_SIZE):
        chunk = ",".join(ids[i:i + BATCH_SIZE])
        for d in http_get_json(f"{GAMES_BASE}/v1/games?universeIds={chunk}").get("data", []):
            games[str(d["id"])] = d
        time.sleep(REQUEST_GAP_SEC)
        for d in http_get_json(f"{GAMES_BASE}/v1/games/votes?universeIds={chunk}").get("data", []):
            votes[str(d["id"])] = d
        time.sleep(REQUEST_GAP_SEC)

    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    now_local = now_utc.astimezone().replace(tzinfo=None)
    out = []
    for uid in ids:
        g = games.get(uid)
        if not g:
            log(f"  응답 없음 (삭제·비공개?): {labels[uid]} ({uid})")
            continue
        v = votes.get(uid, {})
        up, down = v.get("upVotes"), v.get("downVotes")
        like = round(100 * up / (up + down), 1) if up is not None and down is not None and up + down else ""
        out.append({
            "time_utc": now_utc.isoformat().replace("+00:00", "Z"),
            "time_local": now_local.isoformat(sep=" "),
            "universe_id": uid,
            "label": labels[uid],
            "playing": g.get("playing", ""),
            "visits": g.get("visits", ""),
            "favorites": g.get("favoritedCount", ""),
            "up_votes": "" if up is None else up,
            "down_votes": "" if down is None else down,
            "like_pct": like,
            "max_players": g.get("maxPlayers", ""),
            "game_updated_utc": g.get("updated", ""),
            "name_now": g.get("name", ""),
        })

    os.makedirs(SNAP_DIR, exist_ok=True)
    path = month_file(SNAP_DIR, now_utc)
    new_file = not os.path.exists(path)
    # utf-8-sig: 엑셀에서 열어도 한글이 안 깨지게
    with open(path, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(out)

    total = sum(int(r["playing"] or 0) for r in out)
    top = max(out, key=lambda r: int(r["playing"] or 0)) if out else None
    log(f"스냅샷 저장: {len(out)}개 게임, 합계 동접 {total:,}"
        + (f", 1위 {top['label']} {int(top['playing']):,}" if top else ""))
    return len(out)


def cmd_run(interval_min, once):
    if once:
        # 1회 모드 (클라우드 실행도 이것). 실패하면 종료 코드 1 → GitHub에서 빨간 X로 보임
        try:
            saved = take_snapshot()
        except Exception as e:
            log(f"  이번 회차 실패: {e}")
            sys.exit(1)
        if not saved:
            sys.exit(1)
        return
    log(f"추적 시작 (간격 {interval_min}분, 저장 위치 {SNAP_DIR})")
    while True:
        started = time.time()
        try:
            take_snapshot()
        except Exception as e:  # 한 번 실패해도 다음 차례에 다시 시도
            log(f"  이번 회차 실패: {e}")
        sleep_for = max(5, interval_min * 60 - (time.time() - started))
        time.sleep(sleep_for)


# ---------------------------------------------------------------- 클라우드 데이터 받기
def cmd_pull(hours=24 * 62):
    """GitHub 저장소의 data/snapshots/ 에서 필요한 달 파일을 data/cloud/ 로 내려받는다."""
    if not CLOUD_REPO:
        log("CLOUD_REPO가 비어 있어 클라우드 데이터를 받을 수 없습니다 (tracker.py 설정 부분).")
        return False
    now = dt.datetime.now(dt.timezone.utc)
    os.makedirs(CLOUD_DIR, exist_ok=True)
    got = 0
    for m in months_between(now - dt.timedelta(hours=hours), now):
        name = f"{m:%Y-%m}.csv"
        url = f"https://raw.githubusercontent.com/{CLOUD_REPO}/{CLOUD_BRANCH}/data/snapshots/{name}"
        req = urllib.request.Request(url, headers={"User-Agent": "rbx-market-tracker/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue          # 그 달 데이터가 아직 없음
            log(f"  {name} 받기 실패: HTTP {e.code}")
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log(f"  {name} 받기 실패: {e}")
            continue
        with open(os.path.join(CLOUD_DIR, name), "wb") as f:
            f.write(body)
        got += 1
    log(f"클라우드 데이터 {got}개 파일 받음 → {CLOUD_DIR}")
    return got > 0


# ---------------------------------------------------------------- 요약
def cmd_summary(hours, local):
    src = SNAP_DIR
    if not local and CLOUD_REPO:
        if cmd_pull(hours):
            src = CLOUD_DIR
        else:
            print("클라우드 데이터를 못 받아서 이 PC의 데이터로 요약합니다.")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    files = [month_file(src, m) for m in months_between(cutoff, dt.datetime.now(dt.timezone.utc))]
    files = [p for p in files if os.path.exists(p)]
    if not files:
        sys.exit("아직 데이터가 없습니다. 먼저 tracker.py를 실행하세요.")
    by_game = {}
    for p in files:
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                by_game.setdefault(r["universe_id"], []).append(r)
    lines = []
    for uid, rs in by_game.items():
        rs.sort(key=lambda r: r["time_utc"])
        win = [r for r in rs if dt.datetime.fromisoformat(r["time_utc"].replace("Z", "+00:00")) >= cutoff]
        if not win:
            continue
        first, last = win[0], win[-1]
        t0 = dt.datetime.fromisoformat(first["time_utc"].replace("Z", "+00:00"))
        t1 = dt.datetime.fromisoformat(last["time_utc"].replace("Z", "+00:00"))
        span_h = (t1 - t0).total_seconds() / 3600
        peak = max(int(r["playing"] or 0) for r in win)
        avg = sum(int(r["playing"] or 0) for r in win) / len(win)
        visits_gain = int(last["visits"] or 0) - int(first["visits"] or 0)
        per_day = visits_gain / span_h * 24 if span_h >= 1 else None
        lines.append((peak, last["label"], int(last["playing"] or 0), peak, avg, per_day, last["like_pct"], len(win)))

    lines.sort(reverse=True)
    print(f"데이터: {src}")
    print(f"최근 {hours}시간 요약 (동접 최고치 순)")
    print(f"{'게임':<28}{'지금 동접':>12}{'최고':>12}{'평균':>12}{'방문/일':>14}{'좋아요%':>8}{'표본':>6}")
    for _, label, now, peak, avg, per_day, like, n in lines:
        pd_s = f"{per_day:,.0f}" if per_day is not None else "(1h 미만)"
        print(f"{label[:27]:<28}{now:>12,}{peak:>12,}{avg:>12,.0f}{pd_s:>14}{like:>8}{n:>6}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="로블록스 게임 수치 추적기")
    p.add_argument("--once", action="store_true", help="한 번만 찍고 종료")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_MIN, help="간격(분), 기본 10")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("add", help="게임 추가")
    a.add_argument("target", help="게임 주소 또는 플레이스 ID")
    a.add_argument("label", nargs="?", default="", help="표시 이름 (생략하면 게임 이름)")
    sub.add_parser("list", help="감시 목록 보기")
    s = sub.add_parser("summary", help="요약 보기")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--local", action="store_true", help="클라우드 말고 이 PC에서 찍은 데이터로")
    sub.add_parser("pull", help="클라우드(GitHub) 데이터 내려받기")
    args = p.parse_args()

    if args.cmd == "add":
        cmd_add(args.target, args.label)
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "summary":
        cmd_summary(args.hours, args.local)
    elif args.cmd == "pull":
        cmd_pull()
    else:
        if args.interval < 1:
            sys.exit("간격은 1분 이상으로 해 주세요 (레이트 리밋 배려).")
        try:
            cmd_run(args.interval, args.once)
        except KeyboardInterrupt:
            log("사용자가 중지했습니다.")


if __name__ == "__main__":
    main()
