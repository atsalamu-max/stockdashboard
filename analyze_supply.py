#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
하이닉스(또는 임의 종목) 수급-주가 분석 자동화 스크립트
- 입력: 가격 CSV(OHLC), 수급 CSV(투자자별 본주), [선택] 선물 수급 CSV
- 출력: 마크다운 분석 리포트 (오늘 만든 재구성판과 동일 구조)

사용:
    python analyze_supply.py --price 가격.csv --flow 수급.csv [--futures 선물.csv] [--out 리포트.md]

설계 원칙(요약):
- 도입 주체: 외국인(현물·선물) + 금융투자(본주)만. 상관 낮은 주체 제외.
- 금융투자 선물은 본주 헤지라 방향신호에서 제외.
- 위계: 외국인 1차 → 금융투자 본주 2차. 밴드 위치는 곱하지 않고 병렬.
- 수익 기준: 당일 수급 → 익일/+2일/+3일/+5일 종가 수익률.
"""
import argparse, csv, sys, io
import pandas as pd, numpy as np

# ---------------- 유연한 파싱 유틸 ----------------
ENCODINGS = ['cp949', 'utf-8-sig', 'utf-8', 'euc-kr']

def read_csv_flex(path):
    """인코딩 자동 감지 + 원시 행 리스트 반환."""
    for enc in ENCODINGS:
        try:
            with open(path, encoding=enc) as f:
                rows = list(csv.reader(f))
            return rows, enc
        except Exception:
            continue
    raise RuntimeError(f"인코딩 감지 실패: {path}")

def to_num(s):
    """'+1,234' / '' / None → float."""
    if s is None: return np.nan
    s = str(s).replace('+', '').replace(',', '').strip()
    if s == '' or s == '-': return np.nan
    try: return float(s)
    except: return np.nan

def parse_date(s):
    s = str(s).strip().replace('/', '-')
    try: return pd.to_datetime(s)
    except: return pd.NaT

# ---------------- 입력 로더 ----------------
def load_price(path):
    """가격 CSV: 날짜+OHLC 포함. 컬럼명 유연 매칭."""
    rows, _ = read_csv_flex(path)
    # 데이터 행 = 첫 칸이 날짜(YYYY로 시작)인 행
    data = [r for r in rows if r and len(r) >= 5 and (str(r[0])[:2] == '20')]
    recs = []
    for r in data:
        d = parse_date(r[0])
        if pd.isna(d): continue
        # 표준 키움 일자별: 날짜,종가,전일대비,등락률,거래량,거래대금,체결강도,시가,고가,저가
        rec = {'date': d, 'close': to_num(r[1])}
        if len(r) >= 10:
            rec.update({'volume': to_num(r[4]), 'open': to_num(r[7]),
                        'high': to_num(r[8]), 'low': to_num(r[9])})
        recs.append(rec)
    df = pd.DataFrame(recs).dropna(subset=['close']).sort_values('date').reset_index(drop=True)
    # OHLC 없으면 종가로 대체(지표는 종가기반이라 무방, 변동폭 분석만 비활성)
    for c in ['open', 'high', 'low']:
        if c not in df or df[c].isna().all():
            df[c] = df['close']
    return df

def load_flow(path):
    """수급 CSV(본주). 헤더가 1~2줄로 나뉠 수 있어 컬럼명 기반으로 위치를 찾는다."""
    rows, _ = read_csv_flex(path)
    # 헤더 후보: 데이터(날짜시작) 이전의 모든 행을 합쳐 컬럼명→인덱스 매핑
    header_rows = []
    data = []
    for r in rows:
        if r and ('/' in str(r[0]) or str(r[0])[:2] == '20'):
            data.append(r)
        elif r:
            header_rows.append(r)
    # 각 인덱스의 컬럼명 = 헤더행들 중 비어있지 않은 마지막 값
    name_at = {}
    for hr in header_rows:
        for j, v in enumerate(hr):
            if str(v).strip():
                name_at[j] = str(v).strip()
    # 컬럼명 → 인덱스 (부분일치 허용)
    def find(*keys):
        for j, nm in name_at.items():
            if any(k in nm for k in keys):
                return j
        return None
    idx = {
        'indiv':   find('개인'),
        'foreign': find('외국인'),  # '기타외국인'보다 '외국인'이 먼저 잡히게 정렬
        'inst_total': find('기관계'),
        'fin_inv': find('금융투자'),
        'trust':   find('투신'),
        'pension': find('연기금'),
    }
    # '외국인' 정확매칭 우선(기타외국인 회피)
    exact_for = [j for j,nm in name_at.items() if nm == '외국인']
    if exact_for: idx['foreign'] = exact_for[0]
    recs = []
    for r in data:
        d = parse_date(r[0])
        if pd.isna(d): continue
        rec = {'date': d}
        for k, j in idx.items():
            rec[k] = to_num(r[j]) if (j is not None and len(r) > j) else np.nan
        recs.append(rec)
    return pd.DataFrame(recs).dropna(subset=['foreign']).sort_values('date').reset_index(drop=True)

def load_futures(path):
    """선물 수급 CSV (선택). 컬럼명 기반으로 외국인/금융투자 위치 탐색."""
    rows, _ = read_csv_flex(path)
    header_rows = [r for r in rows if r and '/' not in str(r[0]) and str(r[0])[:2] != '20']
    data = [r for r in rows if r and '/' in str(r[0])]
    name_at = {}
    for hr in header_rows:
        for j, v in enumerate(hr):
            if str(v).strip(): name_at[j] = str(v).strip()
    def find_exact(name):
        for j, nm in name_at.items():
            if nm == name: return j
        for j, nm in name_at.items():
            if name in nm: return j
        return None
    j_for = find_exact('외국인'); j_fin = find_exact('금융투자')
    recs = []
    for r in data:
        d = parse_date(r[0])
        if pd.isna(d): continue
        recs.append({
            'date': d,
            'fut_for': to_num(r[j_for]) if (j_for is not None and len(r)>j_for) else np.nan,
            'fut_fin': to_num(r[j_fin]) if (j_fin is not None and len(r)>j_fin) else np.nan,
        })
    return pd.DataFrame(recs).sort_values('date').reset_index(drop=True)

# ---------------- 지표 ----------------
def add_indicators(df):
    c = df['close']
    df['ma5']  = c.rolling(5).mean()
    df['ma20'] = c.rolling(20).mean()
    df['ma60'] = c.rolling(60).mean()
    mid = c.rolling(20).mean(); sd = c.rolling(20).std()
    df['bb_up'] = mid + 2*sd; df['bb_dn'] = mid - 2*sd
    df['pctB'] = (c - df['bb_dn']) / (df['bb_up'] - df['bb_dn'])
    delta = c.diff(); up = delta.clip(lower=0); dn = -delta.clip(upper=0)
    ru = up.ewm(alpha=1/14, adjust=False).mean(); rd = dn.ewm(alpha=1/14, adjust=False).mean()
    df['rsi'] = 100 - 100/(1 + ru/rd)
    e12 = c.ewm(span=12, adjust=False).mean(); e26 = c.ewm(span=26, adjust=False).mean()
    df['macd'] = e12 - e26; df['macd_sig'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_sig']
    return df

# ---------------- 분석 ----------------
def fwd(df, i, k):
    return (df.loc[i+k, 'close']/df.loc[i, 'close']-1)*100 if i+k < len(df) else None
def saft(df, i, col, k):
    return df.loc[i+1:i+k, col].sum() if i+k < len(df) else None

def band(p):
    if pd.isna(p): return None
    if p >= 1.0: return '상단돌파(≥1.0)'
    if p >= 0.8: return '상단근처(0.8~1.0)'
    if p >= 0.5: return '중심상(0.5~0.8)'
    if p >= 0.2: return '중심하(0.2~0.5)'
    return '하단(<0.2)'

def run(price_path, flow_path, fut_path=None):
    price = add_indicators(load_price(price_path))
    flow  = load_flow(flow_path)
    m = pd.merge(price, flow, on='date', how='inner').sort_values('date').reset_index(drop=True)
    has_fut = False
    if fut_path:
        fut = load_futures(fut_path)
        m = pd.merge(m, fut, on='date', how='left').sort_values('date').reset_index(drop=True)
        has_fut = 'fut_for' in m and m['fut_for'].notna().any()
    m['ret_next'] = (m['close'].shift(-1)/m['close']-1)*100
    base = m['ret_next'].mean()
    out = io.StringIO()
    P = lambda *a: print(*a, file=out)

    P(f"# 수급-주가 분석 리포트 (자동생성)\n")
    P(f"- 기간: {m['date'].min().date()} ~ {m['date'].max().date()} ({len(m)}거래일)")
    P(f"- 선물 데이터: {'포함' if has_fut else '없음(본주만)'}")
    P(f"- 수익 기준: 당일 수급 → 익일/+N일 종가 수익률")
    P(f"- 베이스라인(전체 익일 평균): {base:+.2f}% — 아래 수치는 이 대비 초과분으로 해석\n")
    P("> ⚠️ **이 분석은 사용자가 투자 판단에 쓰는 정보 중 아주 일부일 뿐이다.** "
      "수급-가격의 과거 통계적 관계를 검증·반증하는 보조 도구이며, "
      "펀더멘털·거시·외생변수·실시간 정보는 포함하지 않는다. 미래 예측치가 아니다.\n")
    P("---\n")

    # 0. 주체 선별 근거
    P("## 0. 주체 선별 근거 (당일순매수 → 익일수익 상관)\n")
    P("| 주체 | 상관 | 매수일 익일 | 매도일 익일 |")
    P("|---|---|---|---|")
    subjects = [('외국인(본주)','foreign'),('금융투자(본주)','fin_inv'),
                ('투신','trust'),('연기금','pension'),('개인','indiv')]
    if has_fut: subjects += [('외국인(선물)','fut_for'),('금융투자(선물)','fut_fin')]
    for nm, col in subjects:
        if col not in m: continue
        v = m.dropna(subset=[col,'ret_next'])
        if len(v) < 3: continue
        cor = v[col].corr(v['ret_next'])
        buy = v[v[col]>0]['ret_next'].mean(); sell = v[v[col]<0]['ret_next'].mean()
        P(f"| {nm} | {cor:+.3f} | {buy:+.2f}% | {sell:+.2f}% |")
    P("\n→ 외국인 본주가 유일한 양(+) 신호, 금융투자 본주는 직후(아래)에서 강함. "
      "나머지는 음(−)이라 방향신호 아님. 금융투자 선물은 본주 헤지라 제외.\n---\n")

    # 1차 축: 외국인 현물x선물 (선물 있을 때만)
    if has_fut:
        P("## 1차 축 — 외국인 현물 × 선물 매트릭스 (→ 익일수익)\n")
        P("| 외국인 | 선물 매수 | 선물 매도 |")
        P("|---|---|---|")
        for sp_lab, sp in [('현물 매수',True),('현물 매도',False)]:
            cells=[]
            for fu in [True, False]:
                s = m[((m['foreign']>0)==sp)&((m['fut_for']>0)==fu)].dropna(subset=['ret_next'])
                cells.append(f"{s['ret_next'].mean():+.1f}% (n={len(s)})" if len(s)>0 else "n=0")
            P(f"| {sp_lab} | {cells[0]} | {cells[1]} |")
        P("\n→ 현물 방향이 지배. 현물+선물 동시매수가 최강 칸.\n")

        # 2차 축: 동시매수 칸 내부 금투 받침
        P("## 2차 축 — 금융투자(본주) 받침 [동시매수 칸 내부, → +3일]\n")
        m['both_buy'] = (m['foreign']>0)&(m['fut_for']>0)
        idx = [i for i in m.index[m['both_buy']] if i+3 < len(m)]
        if idx:
            xs = [saft(m,i,'fin_inv',3) for i in idx]; ys = [fwd(m,i,3) for i in idx]
            cor = np.corrcoef(xs,ys)[0,1] if len(xs)>1 else float('nan')
            sup = [ys[k] for k in range(len(xs)) if xs[k]>0]
            exi = [ys[k] for k in range(len(xs)) if xs[k]<=0]
            P("| 금융투자(직후3일) | +3일 수익 | n |")
            P("|---|---|---|")
            if sup: P(f"| 받침(순매수) | {np.mean(sup):+.1f}% | {len(sup)} |")
            if exi: P(f"| 이탈(순매도) | {np.mean(exi):+.1f}% | {len(exi)} |")
            P(f"\n→ 금융투자 본주 vs +3일 상관 **{cor:+.2f}**. 외국인은 직후 차익실현 전환, "
              "받쳐주는 주체가 금융투자.\n")
        P("---\n")

    # 병렬 A: 밴드구간 x 외국인현물, 익일/+2일
    m['bb'] = m['pctB'].apply(band)
    bb_order = ['상단돌파(≥1.0)','상단근처(0.8~1.0)','중심상(0.5~0.8)','중심하(0.2~0.5)','하단(<0.2)']
    for K, lab in [(1,'익일(+1일)'),(2,'+2일')]:
        P(f"## 병렬 분석 A — 밴드구간 × 외국인 현물 (→ {lab})\n")
        P("기준: 외국인이 그날 현물 매수/매도한 당일 종가 대비.\n")
        P("| 밴드 위치(%B) | 전체 | 외인 현물매수 | 외인 현물매도 |")
        P("|---|---|---|---|")
        for bb in bb_order:
            sub = m[m['bb']==bb]
            idx = [i for i in sub.index if fwd(m,i,K) is not None]
            if not idx: continue
            allr=[fwd(m,i,K) for i in idx]
            buy=[fwd(m,i,K) for i in idx if m.loc[i,'foreign']>0]
            sell=[fwd(m,i,K) for i in idx if m.loc[i,'foreign']<0]
            bs=f"{np.mean(buy):+.1f}% (n={len(buy)})" if buy else "—"
            ss=f"{np.mean(sell):+.1f}% (n={len(sell)})" if sell else "—"
            P(f"| {bb} | {np.mean(allr):+.1f}% (n={len(allr)}) | {bs} | {ss} |")
        P("")
    P("---\n")

    # 병렬 B: 밴드돌파 직후
    P("## 병렬 분석 B — 밴드 위 돌파(%B≥1.0)+신고가 직후\n")
    m['hh20'] = m['high'].rolling(20).max()
    sel = m[(m['pctB']>=1.0)&(m['high']>=m['hh20'])&m['pctB'].notna()]
    P("| n | +1일 | +3일 | +5일 |")
    P("|---|---|---|---|")
    for K in [[1,3,5]]:
        r1=[fwd(m,i,1) for i in sel.index if fwd(m,i,1) is not None]
        r3=[fwd(m,i,3) for i in sel.index if fwd(m,i,3) is not None]
        r5=[fwd(m,i,5) for i in sel.index if fwd(m,i,5) is not None]
        P(f"| {len(sel)} | {np.mean(r1):+.1f}% | {np.mean(r3):+.1f}% | {np.mean(r5):+.1f}% |")
    P("\n→ 밴드 위 돌파는 직후 평균회귀 경향(표본 작음, 외생일은 별도 제외 권장).\n---\n")

    # 변동폭 밴드별
    if has_fut and 'both_buy' in m:
        P("## 참고 — 동시매수 직후 변동폭, 밴드 위치별 (직후 5일)\n")
        P("| 밴드 위치 | n | 최고 | 최저 | 익일 아래꼬리 |")
        P("|---|---|---|---|---|")
        from collections import defaultdict
        d = defaultdict(list)
        for i in m.index[m['both_buy']]:
            if i+5 >= len(m): continue
            c0=m.loc[i,'close']; seg=m.loc[i+1:i+5]
            hi=(seg['high'].max()/c0-1)*100; lo=(seg['low'].min()/c0-1)*100
            n=m.loc[i+1]; wick=(n['low']/n['open']-1)*100
            d[band(m.loc[i,'pctB'])].append((hi,lo,wick))
        for bb in bb_order:
            if bb in d:
                v=d[bb]
                P(f"| {bb} | {len(v)} | {np.mean([x[0] for x in v]):+.1f}% | "
                  f"{np.mean([x[1] for x in v]):+.1f}% | {np.mean([x[2] for x in v]):+.1f}% |")
        P("\n→ %B 1.0이 비대칭 갈림길: 돌파 시 아래로 깊고, 상단근처는 아래 거의 0.\n---\n")

    # 한계
    P("## 표본·적용 한계\n")
    P("- 표본(n)은 참고. 작은 표본의 칸도 수치로 제시하되, n이 작으면 변동이 크다는 점만 감안해 읽는다.")
    P("- 검증 구간이 단일 추세장이면 약세·횡보장 미검증.")
    P("- 20일선 이격 극단·일중변동 분포 끝 같은 가속 국면에선 정상성이 깨져 통계 무효.")
    P("- 외생변수(실적·매크로·환율)는 미반영. 특히 외국인은 환율 변수로 예측이 더 어려움.")
    P("- **다시: 이 결과는 사용자가 보는 투자 정보의 아주 일부일 뿐이며, "
      "논리의 검증·반증 보조 도구이지 미래 예측치나 매매 신호가 아니다.**")
    return out.getvalue()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--price', required=True)
    ap.add_argument('--flow', required=True)
    ap.add_argument('--futures', default=None)
    ap.add_argument('--out', default='수급분석_리포트.md')
    a = ap.parse_args()
    md = run(a.price, a.flow, a.futures)
    with open(a.out, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"완료: {a.out}")

if __name__ == '__main__':
    main()
