"""
게임 프로필 수집기 (하루 1회)

tracker.py가 "누가 이겼나"(동접·방문·좋아요)를 10분마다 찍는다면,
이 파일은 "왜 이겼나"의 후보, 즉 플레이어가 들어오기 전에 보는 것과 운영 흔적을 하루 한 번 찍는다.

모으는 것 (전부 로블록스 공개 API, 비밀값 없음)
- 진열대: 이름, 아이콘, 썸네일, 설명
- 타이밍: 생성일, 같은 장르(watchlist 메모의 첫 칸)에서 가장 먼저 만든 게임보다 며칠 늦었나
- 제작자: 개인/그룹, 그룹 인원, 같은 제작자의 다른 공개 게임 수와 그중 최고 방문 수
- 수익화: 게임패스 개수와 가격
업데이트 간격은 tracker.py 스냅샷의 game_updated_utc 열에 이미 10분 단위로 쌓이므로 여기서는 따로 이력을 만들지 않는다.

저장
- data/profiles/YYYY-MM-DD.csv : 그날 게임별 한 줄
- data/profiles/changes.csv     : 전날과 비교해 이름·아이콘·썸네일·설명·게임패스가 바뀐 기록 (누적)

실행: python3 profiler.py   (클라우드에서는 .github/workflows/profile.yml 이 하루 한 번 돌림)
"""
import csv
import datetime as dt
import glob
import hashlib
import os
import statistics
import sys
import time
import urllib.parse

import tracker
from tracker import http_get_json, load_watchlist, log

GAP_SEC = 0.5                      # 요청 사이 쉬는 시간
DESC_KEEP = 300                    # 설명은 앞부분만 저장 (전체 변화는 해시로 잡음)
PROFILE_DIR = os.path.join(tracker.DATA_DIR, "profiles")
CHANGES_FILE = os.path.join(PROFILE_DIR, "changes.csv")

GAMES = tracker.GAMES_BASE
APIS = tracker.APIS_BASE
THUMBS = os.environ.get("RBX_THUMBS_BASE", "https://thumbnails.roblox.com")
GROUPS = os.environ.get("RBX_GROUPS_BASE", "https://groups.roblox.com")

FIELDS = [
    "date_utc", "universe_id", "label", "genre", "role",
    "name", "genre_l1", "genre_l2", "created_utc", "updated_utc",
    "days_since_created", "genre_order", "days_after_genre_first",
    "max_players", "price",
    "creator_type", "creator_id", "creator_name", "group_members",
    "creator_public_games", "creator_other_best_name", "creator_other_best_visits",
    "creator_other_total_visits",
    "gamepass_count", "gamepass_min", "gamepass_median", "gamepass_max", "gamepass_prices",
    "icon_id", "thumbnail_count", "thumbnail_ids",
    "description_len", "description_hash", "description",
]
# 전날과 비교해 바뀌면 changes.csv에 남길 칸
WATCH_CHANGES = ["name", "icon_id", "thumbnail_ids", "description_hash",
                 "gamepass_count", "gamepass_prices", "max_players", "genre_l2", "creator_id"]


def get(url):
    time.sleep(GAP_SEC)
    return http_get_json(url)


def chunks(xs, n=50):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def icon_key(url):
    # https://tr.rbxcdn.com/180DAY-<해시>/512/512/... → 해시 부분만 (아이콘이 바뀌면 해시가 바뀜)
    if not url:
        return ""
    part = url.split("/")[3] if url.count("/") >= 3 else url
    return part.split("-", 1)[-1]


def fetch_games(ids):
    out = {}
    for c in chunks(ids):
        for d in get(f"{GAMES}/v1/games?universeIds={','.join(c)}").get("data", []):
            out[str(d["id"])] = d
    return out


def fetch_icons(ids):
    out = {}
    for c in chunks(ids):
        url = f"{THUMBS}/v1/games/icons?universeIds={','.join(c)}&size=512x512&format=Png&isCircular=false"
        for d in get(url).get("data", []):
            out[str(d["targetId"])] = icon_key(d.get("imageUrl") or "")
    return out


def fetch_thumbs(ids):
    out = {}
    for c in chunks(ids, 20):
        url = (f"{THUMBS}/v1/games/multiget/thumbnails?universeIds={','.join(c)}"
               "&countPerUniverse=10&defaults=false&size=768x432&format=Png&isCircular=false")
        for d in get(url).get("data", []):
            out[str(d["universeId"])] = [str(t["targetId"]) for t in d.get("thumbnails") or []]
    return out


def fetch_passes(uid):
    prices, token = [], ""
    for _ in range(5):                      # 최대 500개
        url = f"{APIS}/game-passes/v1/universes/{uid}/game-passes?passView=Full&pageSize=100"
        if token:
            url += "&pageToken=" + urllib.parse.quote(token)
        j = get(url)
        for p in j.get("gamePasses") or []:
            if p.get("isForSale") and p.get("price") is not None:
                prices.append(int(p["price"]))
        token = j.get("nextPageToken") or ""
        if not token:
            break
    return sorted(prices)


def fetch_creator(ctype, cid):
    """그룹 인원(그룹만)과 그 제작자의 공개 게임 목록 [(universe_id, 이름, 방문)]."""
    members = ""
    if ctype == "Group":
        members = get(f"{GROUPS}/v1/groups/{cid}").get("memberCount", "")
        base = f"{GAMES}/v2/groups/{cid}/gamesV2?accessFilter=Public&limit=50&sortOrder=Desc"
    else:
        base = f"{GAMES}/v2/users/{cid}/games?accessFilter=Public&limit=50&sortOrder=Desc"
    games, cursor = [], ""
    for _ in range(4):                      # 최대 200개
        j = get(base + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else ""))
        for g in j.get("data") or []:
            games.append((str(g.get("id")), g.get("name", ""), int(g.get("placeVisits") or 0)))
        cursor = j.get("nextPageCursor") or ""
        if not cursor:
            break
    return members, games


def build_rows(now):
    wl = load_watchlist()
    ids = [r["universe_id"] for r in wl]
    games = fetch_games(ids)
    icons = fetch_icons(ids)
    thumbs = fetch_thumbs(ids)

    # 장르별 생성 순서 (watchlist 안에서의 순서라 "진짜 원조"가 아닐 수 있음)
    meta = {}
    for r in wl:
        parts = [p.strip() for p in (r.get("memo") or "").split("/")]
        meta[r["universe_id"]] = (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")
    created = {u: parse_time(games[u].get("created")) for u in ids if u in games}
    by_genre = {}
    for u, c in created.items():
        by_genre.setdefault(meta[u][0], []).append((c, u))
    order, first = {}, {}
    for g, xs in by_genre.items():
        xs.sort()
        first[g] = xs[0][0]
        for i, (_, u) in enumerate(xs, 1):
            order[u] = i

    creators = {}
    rows = []
    for r in wl:
        u = r["universe_id"]
        d = games.get(u)
        if not d:
            log(f"  {r['label']}: 게임 정보를 못 받음 (삭제·비공개일 수 있음)")
            continue
        genre, role = meta[u]
        cr = d.get("creator") or {}
        ckey = (cr.get("type", ""), str(cr.get("id", "")))
        if ckey not in creators:
            try:
                creators[ckey] = fetch_creator(*ckey)
            except Exception as e:
                log(f"  제작자 {ckey} 정보 실패: {e}")
                creators[ckey] = ("", None)
        members, cgames = creators[ckey]
        others = [g for g in (cgames or []) if g[0] != u]
        best = max(others, key=lambda g: g[2]) if others else None
        try:
            prices = fetch_passes(u)
            pass_cols = [len(prices), prices[0] if prices else "", int(statistics.median(prices)) if prices else "",
                         prices[-1] if prices else "", ";".join(map(str, prices))]
        except Exception as e:
            log(f"  {r['label']}: 게임패스 실패: {e}")
            pass_cols = ["", "", "", "", ""]
        desc = d.get("sourceDescription") or d.get("description") or ""
        c = created.get(u)
        rows.append(dict(zip(FIELDS, [
            f"{now:%Y-%m-%d}", u, r["label"], genre, role,
            d.get("sourceName") or d.get("name", ""), d.get("genre_l1", ""), d.get("genre_l2", ""),
            d.get("created", ""), d.get("updated", ""),
            (now - c).days if c else "", order.get(u, ""),
            (c - first[genre]).days if c and genre in first else "",
            d.get("maxPlayers", ""), d.get("price") if d.get("price") is not None else "",
            ckey[0], ckey[1], cr.get("name", ""), members,
            len(cgames) if cgames is not None else "",
            best[1] if best else "", best[2] if best else "",
            sum(g[2] for g in others) if cgames is not None else "",
            *pass_cols,
            icons.get(u, ""), len(thumbs.get(u, [])), ";".join(thumbs.get(u, [])),
            len(desc), hashlib.sha1(desc.encode("utf-8")).hexdigest()[:10],
            " ".join(desc.split())[:DESC_KEEP],
        ])))
    return rows


def previous_rows(today_name):
    files = sorted(p for p in glob.glob(os.path.join(PROFILE_DIR, "????-??-??.csv"))
                   if os.path.basename(p) < today_name)
    if not files:
        return {}
    with open(files[-1], encoding="utf-8-sig", newline="") as f:
        return {r["universe_id"]: r for r in csv.DictReader(f)}


def main():
    now = dt.datetime.now(dt.timezone.utc)
    rows = build_rows(now)
    if not rows:
        log("프로필 0개: 저장하지 않음")
        sys.exit(1)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    name = f"{now:%Y-%m-%d}.csv"
    prev = previous_rows(name)

    with open(os.path.join(PROFILE_DIR, name), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    changes = []
    for r in rows:
        p = prev.get(r["universe_id"])
        if not p:
            continue
        for k in WATCH_CHANGES:
            if str(p.get(k, "")) != str(r[k]):
                changes.append({"date_utc": r["date_utc"], "universe_id": r["universe_id"], "label": r["label"],
                                "field": k, "old": p.get(k, ""), "new": r[k]})
    if changes:
        new_file = not os.path.exists(CHANGES_FILE)
        with open(CHANGES_FILE, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date_utc", "universe_id", "label", "field", "old", "new"])
            if new_file:
                w.writeheader()
            w.writerows(changes)
    log(f"프로필 저장: {len(rows)}개 게임, 전날 대비 변경 {len(changes)}건")


if __name__ == "__main__":
    main()
