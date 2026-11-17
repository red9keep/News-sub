#!/usr/bin/env bash
# AlphaScope 수집 중 GitHub(해외 데이터센터) 주소로는 막히는 세 곳이 집 IP(VPN)로는 열리는지 본다 (2026-11-17, trial.yml mode=home-ip).
# 같은 요청을 바로(GitHub 주소) · VPN(집 IP) 두 번 보내 상태 코드 · 크기 · 내용 확인을 비교한다.
# 그 뒤 HOLD 초 동안 VPN 을 붙잡고 있다가 다시 집 IP 로 나가는지 본다 — matrix 두 접속이 서로를 끊는지 확인용.
# 주소(IP)는 찍지 않는다(vpn-up.sh 가 add-mask 함).
set -uo pipefail
SRC="${1:-}"
HOLD="${HOLD:-90}"
TAG="${CONN:-1}"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MIRAE="https://investments.miraeasset.com/tigeretf"
KSD="KR7360750004"   # TIGER 미국S&P500 (360750)

probe() {   # 이름 · 확인 낱말 · curl 인자...
  local name="$1" must="$2"; shift 2
  for via in direct vpn; do
    local extra=()
    if [ "$via" = vpn ]; then
      [ -n "$SRC" ] || { echo "  [$name] vpn   : VPN 주소 없음 — 건너뜀"; continue; }
      extra=(--interface "$SRC")
    fi
    local body; body=$(mktemp)
    local res; res=$(curl -sS -m 40 -A "$UA" "${extra[@]}" -o "$body" -w "%{http_code} %{size_download}B %{time_total}s" "$@" 2>&1 | tail -1)
    local ok="내용 X"; grep -q -- "$must" "$body" && ok="내용 O"
    printf "  [%s] %-6s: %s · %s\n" "$name" "$via" "$res" "$ok"
    rm -f "$body"
    sleep 2
  done
}

echo "=== 접속 $TAG — 세 곳 비교 (바로 = GitHub 주소, vpn = 집 IP) ==="
probe "국민연금 공시 목록" "fnc_goBbsDetail" -X POST -d "cat=QUA&pageIndex=1" "https://fund.nps.or.kr/impa/edwmpblnt/getOHEF0001M0.do"
probe "미래에셋 TIGER 페이지" "총보수" "$MIRAE/ko/product/search/detail/index.do?ksdFund=$KSD"
probe "미래에셋 TIGER 구성" "rtnData" -H "Accept: application/json, text/javascript, */*; q=0.01" \
  -e "$MIRAE/ko/product/search/detail/index.do?ksdFund=$KSD" "$MIRAE/ko/product/chart/prdct-item-list.ajax?ksdFund=$KSD&prfPrd=Week01&listCnt=1000"
probe "마이클 버리 substack" "<rss" "https://michaeljburry.substack.com/feed"

[ -n "$SRC" ] || exit 0
echo "=== 접속 $TAG — ${HOLD}초 붙잡은 뒤 다시 확인 (다른 접속이 이 접속을 끊는지) ==="
sleep "$HOLD"
if ip -4 -o addr show ppp0 >/dev/null 2>&1 && curl -s -m 20 --interface "$SRC" -o /dev/null https://api.ipify.org; then
  echo "  접속 $TAG: ${HOLD}초 뒤에도 집 IP 로 나감 (끊기지 않음)"
else
  echo "::warning::접속 $TAG: ${HOLD}초 뒤 VPN 이 끊김 — 동시 접속에서 다른 접속이 밀어냈을 수 있음"
fi
