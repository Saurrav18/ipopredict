import os, json, re, sys
import numpy as np, pandas as pd
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from enum import Enum
import warnings; warnings.filterwarnings('ignore')

DATASET = os.environ.get("IPO_DATASET",
          "GMP_ML_READY_FINAL_v3.xlsx")

@dataclass
class Observation:
    step: int
    tool: str
    input: Dict
    result: Any
    insight: str

@dataclass
class AgentState:
    question: str = ""
    parsed_intent: Dict = field(default_factory=dict)
    plan: List[str] = field(default_factory=list)
    observations: List[Observation] = field(default_factory=list)
    confidence: float = 0.0
    contradictions: List[str] = field(default_factory=list)
    findings: Dict = field(default_factory=dict)
    step_count: int = 0
    status: str = "starting"
    final_answer: str = ""

    def add(self, tool, inp, result, insight):
        self.step_count += 1
        self.observations.append(Observation(
            step=self.step_count, tool=tool,
            input=inp, result=result, insight=insight))

    def tools_used(self): return {o.tool for o in self.observations}

_data = {}

def _load():
    if _data: return
    from sklearn.preprocessing import RobustScaler, LabelEncoder
    from sklearn.neighbors import NearestNeighbors
    from sklearn.decomposition import PCA

    df = pd.read_excel(DATASET)
    df = df.dropna(subset=['Listing Gain','gmp_closing_gain_pct'])
    df['Date'] = pd.to_datetime(df['Date'], dayfirst=True)
    df = df.sort_values('Date').reset_index(drop=True)
    df['win'] = (df['Listing Gain']>0).astype(int)
    df['big_win'] = (df['Listing Gain']>20).astype(int)
    df['gmp_vs_listing'] = df['Listing Gain'] - df['gmp_closing_gain_pct']
    df['gmp_accurate'] = (df['gmp_vs_listing'].abs() < 10).astype(int)
    df['ipo_year'] = df['Date'].dt.year
    le = LabelEncoder()
    df['sector_code'] = le.fit_transform(df['sector'].fillna('Unknown'))

    RAG_F = ['gmp_closing_gain_pct','Total','QIB','RII','Issue_Size(crores)',
             'Offer Price','pre_issue_pe','roe_ronw','ofs_pct',
             'promoter_holding_post_ipo','sector_code']
    rdf = df[RAG_F].fillna(df[RAG_F].median())
    sr = RobustScaler(); knn_data = sr.fit_transform(rdf)
    knn = NearestNeighbors(n_neighbors=8, metric='cosine', algorithm='brute')
    knn.fit(knn_data)

    PCA_F = ['gmp_closing_gain_pct','Total','QIB','RII','Issue_Size(crores)','sector_code']
    pdf = df[PCA_F].fillna(df[PCA_F].median())
    sp = RobustScaler(); Xp = sp.fit_transform(pdf)
    pca = PCA(n_components=4, random_state=42); pca.fit(Xp)
    all_pca_err = np.array([float(np.mean((pca.inverse_transform(
        pca.transform([r]))-[r])**2)) for r in Xp])
    pca_thr_92 = float(np.percentile(all_pca_err, 92))

    _data.update({
        'df': df, 'le': le, 'knn': knn, 'sr': sr, 'RAG_F': RAG_F,
        'pca': pca, 'sp': sp, 'PCA_F': PCA_F, 'pca_thr': pca_thr_92,
        'all_pca_err': all_pca_err,
    })

def tool_similarity_search(target_features: Dict, n: int = 5):
    """Find n IPOs most similar to a target."""
    _load()
    F = _data['RAG_F']
    df = _data['df']
    medians = df[F].fillna(df[F].median()).median()
    vec = []
    for f in F:
        if f == 'sector_code' and 'sector' in target_features:
            try: vec.append(int(_data['le'].transform([target_features['sector']])[0]))
            except: vec.append(int(medians[f]))
        elif f in target_features:
            vec.append(float(target_features[f]))
        else:
            vec.append(float(medians[f]))
    ds, ix = _data['knn'].kneighbors(_data['sr'].transform([vec]), n_neighbors=n+1)
    results = []
    for d, i in zip(ds[0][1:n+1], ix[0][1:n+1]):
        row = df.iloc[i]
        results.append({
            'name': str(row['IPO_Name']),
            'date': str(row['Date'].date()),
            'sector': str(row['sector']),
            'gmp': float(row['gmp_closing_gain_pct']),
            'qib': float(row['QIB']),
            'sub': float(row['Total']),
            'listing_gain': float(row['Listing Gain']),
            'won': bool(row['Listing Gain'] > 0),
            'similarity': round(float(1-d), 3),
        })
    return {'neighbours': results,
            'avg_listing': round(np.mean([r['listing_gain'] for r in results]), 1),
            'win_rate': round(np.mean([1 if r['won'] else 0 for r in results]), 2)}

def tool_filter_search(filters: Dict, limit: int = 20):
    """Filter IPOs by conditions. filters = {'gmp_min': 7, 'qib_min': 10, ...}"""
    _load()
    df = _data['df'].copy()
    applied = []
    for k, v in filters.items():
        if k == 'gmp_min':       df = df[df['gmp_closing_gain_pct'] >= v]; applied.append(f"GMP>={v}%")
        elif k == 'gmp_max':     df = df[df['gmp_closing_gain_pct'] <= v]; applied.append(f"GMP<={v}%")
        elif k == 'qib_min':     df = df[df['QIB'] >= v]; applied.append(f"QIB>={v}x")
        elif k == 'qib_max':     df = df[df['QIB'] <= v]; applied.append(f"QIB<={v}x")
        elif k == 'sub_min':     df = df[df['Total'] >= v]; applied.append(f"Sub>={v}x")
        elif k == 'rii_min':     df = df[df['RII'] >= v]; applied.append(f"RII>={v}x")
        elif k == 'hni_min':     df = df[df['HNI'] >= v]; applied.append(f"HNI>={v}x")
        elif k == 'price_max':   df = df[df['Offer Price'] <= v]; applied.append(f"price<=Rs{v}")
        elif k == 'price_min':   df = df[df['Offer Price'] >= v]; applied.append(f"price>=Rs{v}")
        elif k == 'sector':      df = df[df['sector'] == v]; applied.append(f"sector={v}")
        elif k == 'listing_min': df = df[df['Listing Gain'] >= v]; applied.append(f"listing>={v}%")
        elif k == 'listing_max': df = df[df['Listing Gain'] <= v]; applied.append(f"listing<={v}%")
        elif k == 'ofs_max':     df = df[df['ofs_pct'] <= v]; applied.append(f"OFS<={v}%")
        elif k == 'ofs_min':     df = df[df['ofs_pct'] >= v]; applied.append(f"OFS>={v}%")
        elif k == 'size_max':    df = df[df['Issue_Size(crores)'] <= v]; applied.append(f"size<={v}cr")
        elif k == 'size_min':    df = df[df['Issue_Size(crores)'] >= v]; applied.append(f"size>={v}cr")
        elif k == 'year':        df = df[df['ipo_year'] == v]; applied.append(f"year={v}")
    return {
        'filters_applied': applied,
        'n_matched': len(df),
        'win_rate': round(float(df['win'].mean()) if len(df)>0 else 0, 3),
        'avg_listing': round(float(df['Listing Gain'].mean()) if len(df)>0 else 0, 1),
        'sample': [{'name': str(r['IPO_Name'])[:30],
                    'gmp': float(r['gmp_closing_gain_pct']),
                    'listing': float(r['Listing Gain'])}
                   for _, r in df.head(limit).iterrows()],
    }

def tool_pattern_analysis(group_by: str = 'sector', min_count: int = 5):
    """Group by sector/year/size_bucket and compute win rate + listing gain."""
    _load()
    df = _data['df']
    if group_by == 'size_bucket':
        df = df.copy()
        df['size_bucket'] = pd.cut(df['Issue_Size(crores)'],
                                    [0, 100, 500, 2000, 99999],
                                    labels=['<100cr','100-500','500-2000','2000+'])
        group_by = 'size_bucket'
    elif group_by == 'sub_bucket':
        df = df.copy()
        df['sub_bucket'] = pd.cut(df['Total'], [0,5,20,100,99999],
                                   labels=['<5x','5-20x','20-100x','100x+'])
        group_by = 'sub_bucket'
    elif group_by == 'month':
        df = df.copy()
        df['month'] = df['Date'].dt.month_name()
        group_by = 'month'
    g = df.groupby(group_by).agg(
        count=('win', 'size'), wr=('win','mean'),
        avg_listing=('Listing Gain','mean'), avg_gmp=('gmp_closing_gain_pct','mean'),
    ).reset_index()
    g = g[g['count'] >= min_count].sort_values('wr', ascending=False)
    return {
        'group_by': group_by,
        'rows': [{'group': str(r[group_by]),
                  'count': int(r['count']),
                  'win_rate': round(float(r['wr']), 3),
                  'avg_listing': round(float(r['avg_listing']), 1),
                  'avg_gmp': round(float(r['avg_gmp']), 1)}
                 for _, r in g.iterrows()],
    }

def tool_correlation(feature1: str, feature2: str):
    """Compute correlation between two features."""
    _load()
    df = _data['df']
    if feature1 not in df.columns or feature2 not in df.columns:
        return {'error': f"feature not found", 'available': list(df.select_dtypes(include='number').columns)[:20]}
    sub = df[[feature1, feature2]].dropna()
    if len(sub) < 10: return {'error': 'not enough data'}
    corr = float(sub.corr().iloc[0,1])
    return {
        'feature1': feature1, 'feature2': feature2,
        'n_samples': len(sub),
        'correlation': round(corr, 3),
        'strength': 'strong' if abs(corr)>0.5 else 'moderate' if abs(corr)>0.3 else 'weak',
    }

def tool_timeline_analysis(year1: int, year2: int):
    """Compare two years."""
    _load()
    df = _data['df']
    y1 = df[df['ipo_year']==year1]; y2 = df[df['ipo_year']==year2]
    def stats(s):
        if len(s)==0: return None
        return {'count':len(s),'wr':round(float(s['win'].mean()),3),
                'avg_listing':round(float(s['Listing Gain'].mean()),1),
                'avg_gmp':round(float(s['gmp_closing_gain_pct'].mean()),1)}
    return {'year1': year1, 'year2': year2,
            year1: stats(y1), year2: stats(y2)}

def tool_signal_check(ipo_features: Dict):
    """Run our 5 ML signals on a hypothetical IPO."""
    _load()
    gmp = ipo_features.get('gmp', 0)
    qib = ipo_features.get('qib', 0)
    sub = ipo_features.get('sub', 0)
    rii = ipo_features.get('rii', 1)
    ofs = ipo_features.get('ofs_pct', 50)
    signals = {}
    signals['signal_1_likely_apply'] = (gmp >= 7 and qib >= 5)
    signals['signal_4_allotment'] = round(min(100, 100/max(rii, 0.1)), 1)
    signals['big_win_likely'] = (gmp >= 20 and qib >= 30)
    signals['caution'] = []
    if ofs >= 90: signals['caution'].append('100% OFS, money to promoters')
    if qib < 1 and gmp > 10: signals['caution'].append('GMP high but QIB cold')
    if sub < 2: signals['caution'].append('Very low subscription')
    return signals

def tool_calibration_query(target_wr: int):
    """Read slider calibration to answer 'what config achieves X% WR'."""
    try:
        with open('alert_calibration.json') as f:
            data = json.load(f)
        cfg = data.get('slider_levels', {}).get(str(target_wr))
        if cfg:
            return {'target': target_wr,
                    'model': cfg['model'], 'threshold': cfg['threshold'],
                    'avg_wr': cfg['avg_wr'], 'min_wr': cfg['min_wr'],
                    'avg_picks': cfg['avg_picks']}
        return {'error': f"no calibration for {target_wr}%"}
    except: return {'error': 'calibration file missing'}

def tool_anomaly_detection(ipo_features: Dict):
    """Run PCA anomaly check on hypothetical IPO."""
    _load()
    vec = []
    for f in _data['PCA_F']:
        if f == 'sector_code' and 'sector' in ipo_features:
            try: vec.append(int(_data['le'].transform([ipo_features['sector']])[0]))
            except: vec.append(0)
        elif f == 'gmp_closing_gain_pct': vec.append(ipo_features.get('gmp', 0))
        elif f == 'Total':                 vec.append(ipo_features.get('sub', 1))
        elif f == 'QIB':                   vec.append(ipo_features.get('qib', 0))
        elif f == 'RII':                   vec.append(ipo_features.get('rii', 1))
        elif f == 'Issue_Size(crores)':    vec.append(ipo_features.get('issue_size', 100))
        else: vec.append(0)
    sc = _data['sp'].transform([vec])
    err = float(np.mean((_data['pca'].inverse_transform(_data['pca'].transform(sc))-sc)**2))
    pct = float((_data['all_pca_err'] <= err).mean())
    return {'pca_error': round(err,4),
            'percentile': round(pct,2),
            'is_anomaly': err > _data['pca_thr'],
            'verdict': ' UNUSUAL, historically rare profile' if err>_data['pca_thr']
                       else '[OK] Normal, fits historical patterns'}

TOOLS = {
    'similarity_search':    tool_similarity_search,
    'filter_search':        tool_filter_search,
    'pattern_analysis':     tool_pattern_analysis,
    'correlation':          tool_correlation,
    'timeline_analysis':    tool_timeline_analysis,
    'signal_check':         tool_signal_check,
    'calibration_query':    tool_calibration_query,
    'anomaly_detection':    tool_anomaly_detection,
}

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

GEMINI_PROMPT = (
    "Convert this IPO question into JSON intent. Respond ONLY with JSON, no markdown.\n"
    '{"type": one of [similarity,comparison,causal,ranking,retrieval,prediction,correlation,lookup,timeline],'
    ' "entities": {"ipo_name": str or null, "sector": str or null},'
    ' "constraints": {optional: gmp_min,gmp_max,qib_min,qib_max,sub_min,rii_min,hni_min,'
    'listing_min,listing_max,year,year1,year2,size_max,size_min,price_max,price_min,ofs_max,ofs_min}}\n'
    "Sectors must be one of: Manufacturing, FMCG/Consumer, Technology, Pharma/Healthcare, BFSI, "
    "Real Estate/Infra, Energy, Hospitality, Logistics, Agriculture, Education, REIT/InvIT, Telecom\n"
    "Question: {q}"
)

def _gemini_parse(q: str, debug: bool = False) -> Optional[Dict]:
    """
    FREE LLM tier - Google Gemini Flash (free: 1,500 requests/day, no credit card).
    Optional Gemini refinement of intent parsing. Key from https://aistudio.google.com

    Returns None on any failure so the caller falls back to rules. Pass
    debug=True (or run this file directly) to see exactly WHY it failed:
    bad key, wrong model name, no network, rate limit, or unparseable reply.
    """
    if not GEMINI_KEY:
        if debug: print("  [gemini] no GEMINI_API_KEY set in environment")
        return None
    import requests, json as _json
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    try:
        r = requests.post(
            url,
            json={"contents": [{"parts": [{"text": GEMINI_PROMPT.format(q=q)}]}],
                  "generationConfig": {"temperature": 0, "maxOutputTokens": 300}},
            timeout=10)
    except requests.exceptions.Timeout:
        if debug: print("  [gemini] request timed out (network slow or blocked)")
        return None
    except requests.exceptions.RequestException as e:
        if debug: print(f"  [gemini] network error: {e}")
        return None

    if r.status_code != 200:
        if debug:
            msg = ""
            try: msg = r.json().get("error", {}).get("message", "")
            except Exception: msg = r.text[:200]
            hint = {400: "bad request or invalid key format",
                    403: "API key invalid or API not enabled",
                    404: f"model '{GEMINI_MODEL}' not found - set GEMINI_MODEL to a valid name",
                    429: "rate limit hit (1,500/day free) - wait and retry"}.get(r.status_code, "")
            print(f"  [gemini] HTTP {r.status_code}: {hint}. {msg}")
        return None

    try:
        body = r.json()
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        if debug:
            reason = body.get("promptFeedback", {}) if isinstance(body, dict) else {}
            print(f"  [gemini] unexpected response shape. feedback={reason}")
        return None

    text = text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = _json.loads(text)
    except _json.JSONDecodeError:
        if debug: print(f"  [gemini] model did not return valid JSON. got: {text[:160]}")
        return None

    parsed.setdefault("entities", {})
    parsed.setdefault("constraints", {})
    parsed["parser"] = "gemini-free"
    if debug: print(f"  [gemini] OK -> {parsed}")
    return parsed

def _rules_parse(q: str) -> Dict:
    """Pure rule-based parser. Always runs first. Cheap."""
    ql = q.lower()
    intent = {
        'type': 'unknown',
        'entities': {},
        'constraints': {},
        'comparisons': [],
        'needs_names': False,
        'needs_timeline': False,
        'parser': 'rules',
    }

    HINGLISH = ['paisa', 'sahi', 'kya', 'hai', 'kaisa', 'kaisi', 'bharo',
                'lagana', 'milega', 'milegi', 'fayda', 'nuksan']
    if any(w in ql for w in HINGLISH):
        intent['type'] = 'prediction'
        intent['entities']['language'] = 'hinglish'

    NAMED_IPOS = ['cmr green','hexagon','tata tech','tata technologies',
                  'paytm','nykaa','zomato','lic','reliance power',
                  'mamaearth','swiggy','ola','indegene','bajaj housing',
                  'ntpc green','hyundai','vishal mega mart','swiggy']
    for name in NAMED_IPOS:
        if re.search(r'\b' + re.escape(name) + r'\b', ql):
            intent['type'] = 'lookup'
            intent['entities']['ipo_name'] = name
            break

    if intent['type'] == 'unknown':
        METRIC_WORDS = ['qib','hni','rii','gmp','promoter','pe ',' pe','roe','ofs','subscription','retail']
        EFFECT_WORDS = ['good sign','bad sign','do badly','do well','matter','important',
                        'affect','help','hurt','better','worse','crash','a good','a bad',
                        'outperform','underperform','what happens','make a difference','guarantee','guarantees','good or bad','bad or good']
        if (any(m in ql for m in METRIC_WORDS) and any(e in ql for e in EFFECT_WORDS)):
            intent['type'] = 'correlation'
        elif any(x in ql for x in ['correlation', 'correlated', 'related to', 'affect', 'impact of', 'effect of', 'reliable', 'accurate', 'trust', 'fake or real']):
            intent['type'] = 'correlation'
        elif any(x in ql for x in ['this year', 'last year', 'bull run', 'bear market',
                                    'season', 'time of year', 'currently', 'right now', 'these days',
                                    'this quarter', 'past few months', 'recent months',
                                    'market hot', 'market cold', 'market temp', 'still rising',
                                    'going to keep', 'drying up', 'q1', 'q2', 'q3', 'q4']):
            intent['type'] = 'timeline'
        elif any(x in ql for x in ['why ', 'underperform', 'fail', 'flop', 'crash', 'lose', 'avoid', 'worry', 'should i worry', 'crash on listing', 'disasters', 'loss-making', 'losing']):
            intent['type'] = 'causal'
        elif any(x in ql for x in ['compare', ' vs ', 'versus', 'better than', 'safer than', 'do better', 'rather than', 'or hold', 'or retail', 'or hni', 'real or']):
            intent['type'] = 'comparison'
        elif any(x in ql for x in ['like ', 'similar to', 'similar ipos', 'comparable to']):
            intent['type'] = 'similarity'
        elif any(x in ql for x in ['best ', 'top ', 'highest', 'which sector', 'which sectors', 'which performs', 'best performing', 'most return', 'biggest', 'any good',
                                    'smallest', 'largest', 'cheapest', 'priciest', 'lowest']):
            intent['type'] = 'ranking'
        elif any(x in ql for x in ['predict', ' will ', 'should i', 'recommend', 'next ipo', 'good buy', 'good investment', 'safe', 'make money', 'how do i', 'making money',
                                    'apply or not', 'worth applying', 'sell on day', 'sell or hold', 'exit on listing', 'worth it', 'good sign', 'bad sign', 'good idea', 'what to expect', 'expect']):
            intent['type'] = 'prediction'
        elif any(x in ql for x in ['average gain', 'average return', 'avg gain', 'overall stats', 'how many ipos', 'baseline', 'every single ipo', 'all ipos']):
            intent['type'] = 'retrieval'
        elif any(x in ql for x in ['find', 'show', 'list', 'which ', 'give me', 'what kind', 'what type', 'still listed']):
            intent['type'] = 'retrieval'
        elif any(x in ql for x in ['performance', 'how did', 'how good', 'track record', 'history of']):
            intent['type'] = 'ranking'

    for m in re.finditer(r'gmp\s*(?:is\s*)?(>=|>|above|over|more than|<=|<|below|less than|of|is)\s*(\d+)', ql):
        op, val = m.group(1), int(m.group(2))
        if any(x in op for x in ['>','above','over','more','is']): intent['constraints']['gmp_min'] = val
        else: intent['constraints']['gmp_max'] = val
    for m in re.finditer(r'qib\s*(?:is\s*)?(>=|>|above|over|more than|<=|<|below|less than|of|is)\s*(\d+)', ql):
        op, val = m.group(1), int(m.group(2))
        if any(x in op for x in ['>','above','over','more','is']): intent['constraints']['qib_min'] = val
        else: intent['constraints']['qib_max'] = val
    for m in re.finditer(r'(\d+)%?\s*(listing\s*gain|return|gain)', ql):
        intent['constraints']['listing_min'] = int(m.group(1))
    for m in re.finditer(r'(gained|gain|returned|listed)\s+(more than|above|over)\s+(\d+)\s*(percent|%)', ql):
        intent['constraints']['listing_min'] = int(m.group(3))
    for m in re.finditer(r'(retail|rii)\s+subscription\s+(above|over|more than)\s+(\d+)', ql):
        intent['constraints']['rii_min'] = int(m.group(3))
    for m in re.finditer(r'hni\s+subscription\s+(above|over|more than)\s+(\d+)', ql):
        intent['constraints']['hni_min'] = int(m.group(2))
    if 'doubled' in ql or 'double' in ql or '2x on listing' in ql:
        intent['constraints']['listing_min'] = 100
    for m in re.finditer(r'(under|below|less than|above|over|more than)\s*(\d+)\s*(crore|cr)\b', ql):
        op, val = m.group(1), int(m.group(2))
        if op in ('under','below','less than'): intent['constraints']['size_max'] = val
        else: intent['constraints']['size_min'] = val
    for m in re.finditer(r'(priced?|price)\s*(under|below|less than|above|over|more than)\s*(?:rs\.?\s*)?(\d+)', ql):
        op, val = m.group(2), int(m.group(3))
        if op in ('under','below','less than'): intent['constraints']['price_max'] = val
        else: intent['constraints']['price_min'] = val
    for m in re.finditer(r'(under|below)\s*(?:rs\.?\s*)?(\d+)\s*rupees', ql):
        intent['constraints']['price_max'] = int(m.group(2))
    for m in re.finditer(r'(lost|fell|dropped|crashed)\s+(more than|above|over)?\s*(\d+)\s*(percent|%)', ql):
        intent['constraints']['listing_max'] = -int(m.group(3))
    years = [int(m.group(1)) for m in re.finditer(r'\b(20\d\d)\b', ql)]
    if len(years) >= 2:
        intent['constraints']['year1'] = years[0]
        intent['constraints']['year2'] = years[1]
        if intent['type'] in ('unknown','comparison'): intent['type'] = 'timeline'
    elif len(years) == 1:
        intent['constraints']['year'] = years[0]
    MONTH_NAMES = ['january','february','march','april',' may ','june','july',
                   'august','september','october','november','december']
    if ('month' in ql or 'seasonal' in ql or 'which season' in ql
        or any(m in ql for m in MONTH_NAMES)):
        intent['constraints']['group_by_month'] = True
        if intent['type'] == 'unknown': intent['type'] = 'timeline'
    if 'so far' in ql or 'how is it going' in ql or 'how are things' in ql:
        if intent['type'] == 'unknown': intent['type'] = 'timeline'
    for m in re.finditer(r'q([1-4])\s*(20\d\d)?', ql):
        intent['constraints']['quarter'] = int(m.group(1))
        if m.group(2): intent['constraints']['year'] = int(m.group(2))

    for name in ['hexagon','cmr green','tega','nykaa','paytm','lic','tata tech',
                 'reliance power','swiggy','zomato','ola']:
        if name in ql: intent['entities']['ipo_name'] = name; break
    sector_map = {'manufacturing':'Manufacturing','fmcg':'FMCG/Consumer','consumer':'FMCG/Consumer',
                  'bfsi':'BFSI','bank':'BFSI','finance':'BFSI',
                  'pharma':'Pharma/Healthcare','healthcare':'Pharma/Healthcare','hospital':'Pharma/Healthcare',
                  'energy':'Energy','renewable':'Energy','solar':'Energy','power':'Energy',
                  'realty':'Real Estate/Infra','real estate':'Real Estate/Infra','infra':'Real Estate/Infra',
                  'agri':'Agriculture','hotel':'Hospitality','hospitality':'Hospitality',
                  'logistic':'Logistics','tech':'Technology','software':'Technology',
                  'education':'Education','edtech':'Education','telecom':'Telecom','reit':'REIT/InvIT','invit':'REIT/InvIT'}
    for sec, full in sector_map.items():
        if sec in ql: intent['entities']['sector'] = full; break

    if intent['type'] == 'unknown' and intent['entities'].get('sector'):
        intent['type'] = 'ranking'
    if intent['type'] == 'unknown' and intent['constraints']:
        intent['type'] = 'retrieval'

    if any(x in ql for x in ['demat', 'hni category', 'retail category', 'allotment',
                              'multiple demat']):
        intent['entities']['strategy_topic'] = 'allotment'
        if intent['type'] == 'unknown':
            intent['type'] = 'prediction'

    return intent

def _is_intent_too_generic(intent: Dict, question: str) -> bool:
    """
    Decide if rules-parsed intent is too generic to give a tailored answer.

    Escalate ONLY when:
      1. Intent type is 'unknown', OR
      2. Type is identified BUT no domain keywords found anywhere in question
         (so the action_builder can't infer anything useful)
    """
    if intent['type'] == 'unknown':
        return True

    ql = question.lower()
    has_useful_signal = (
        bool(intent.get('entities')) or
        bool(intent.get('constraints')) or
        any(w in ql for w in [
            'tech','pharma','manufactur','fmcg','consumer','energy','bfsi','bank',
            'big','large','small','mega','huge premium','most return','100% ofs',
            'fail','flop','lose','bad','low qib','no qib','not subscrib',
            'subscription','sector','size','year','recent','time',
            'gmp','qib','sub ','listing','correlation','correlate','related',
        ])
    )
    if not has_useful_signal:
        return True

    return False

def parse_question(q: str) -> Dict:
    """
    Parse a casual question into a structured intent. Rules run first (instant);
    if the result is too generic and an optional Gemini key is set, Gemini refines
    the intent. Execution stays rule-based either way.
    """
    rules_intent = _rules_parse(q)

    if not _is_intent_too_generic(rules_intent, q):
        return rules_intent

    gemini_result = _gemini_parse(q)
    if gemini_result:
        return {
            'type':        gemini_result.get('type', rules_intent['type']),
            'entities':    {**rules_intent.get('entities',{}),
                            **{k:v for k,v in (gemini_result.get('entities',{}) or {}).items() if v}},
            'constraints': {**rules_intent.get('constraints',{}),
                            **(gemini_result.get('constraints',{}) or {})},
            'parser':      'rules+gemini',
        }

    return rules_intent

def plan_initial(intent: Dict) -> List[str]:
    t = intent['type']
    plan = []
    if t == 'similarity':       plan = ['similarity_search', 'pattern_analysis']
    elif t == 'comparison':     plan = ['filter_search', 'pattern_analysis', 'correlation']
    elif t == 'causal':         plan = ['filter_search', 'pattern_analysis', 'correlation']
    elif t == 'ranking':        plan = ['pattern_analysis', 'filter_search']
    elif t == 'retrieval':      plan = ['filter_search', 'pattern_analysis']
    elif t == 'prediction':     plan = ['filter_search', 'pattern_analysis', 'signal_check', 'similarity_search']
    elif t == 'correlation':    plan = ['correlation', 'pattern_analysis']
    elif t == 'lookup':         plan = ['filter_search', 'similarity_search']
    elif t == 'lookup':         plan = ['similarity_search', 'pattern_analysis']
    elif t == 'timeline':       plan = ['timeline_analysis', 'pattern_analysis']
    else:                       plan = ['filter_search', 'pattern_analysis']

    if intent.get('needs_timeline') and 'timeline_analysis' not in plan:
        plan.append('timeline_analysis')
    if intent.get('needs_names') and 'filter_search' not in plan:
        plan.insert(0, 'filter_search')
    if intent.get('comparisons') and 'pattern_analysis' not in plan:
        plan.append('pattern_analysis')

    return plan

def reflect_and_decide(state: AgentState) -> Optional[Dict]:
    """Returns next action dict, or None if done."""
    obs = state.observations

    if not obs:
        return _build_action(state.plan[0], state)

    last = obs[-1]
    intent = state.parsed_intent

    if last.tool == 'similarity_search' and 'pattern_analysis' not in state.tools_used():
        nb = last.result.get('neighbours', [])
        if nb:
            top_sector = nb[0]['sector']
            state.findings['top_match_sector'] = top_sector
            return {'tool': 'pattern_analysis', 'args': {'group_by': 'sector'}}

    if last.tool == 'filter_search' and last.result.get('n_matched', 0) == 0:
        state.contradictions.append("Initial filter too tight, no matches")
        old = last.input.get('filters', {})
        new = {k:v*0.7 if isinstance(v,(int,float)) and '_min' in k else v
               for k,v in old.items()}
        if new != old:
            return {'tool': 'filter_search', 'args': {'filters': new}}

    if (last.tool == 'filter_search' and last.result.get('n_matched',0) > 0
        and last.result.get('win_rate', 1) < 0.5
        and 'anomaly_detection' not in state.tools_used()):
        state.contradictions.append(f"Filtered IPOs have low WR {last.result['win_rate']:.0%}")
        return {'tool': 'pattern_analysis', 'args': {'group_by': 'sector'}}

    if (intent.get('type') == 'causal'
        and 'correlation' not in state.tools_used()
        and len(obs) >= 2):
        return _build_action('correlation', state)

    if state.step_count >= len(state.plan) + 2:
        state.status = 'synthesizing'
        return None

    used = state.tools_used()
    for step in state.plan:
        if step not in used:
            return _build_action(step, state)

    state.status = 'done'
    return None

def _build_action(tool: str, state: AgentState) -> Dict:
    """Construct tool call args from state.
    Smart: uses intent entities + question text to vary args."""
    intent = state.parsed_intent
    entities = intent.get('entities', {}) or {}
    constraints = intent.get('constraints', {}) or {}
    qlow = state.question.lower()

    if tool == 'similarity_search':
        feats = {}
        for k,v in constraints.items():
            if k=='gmp_min': feats['gmp_closing_gain_pct'] = v
            elif k=='qib_min': feats['QIB'] = v
            elif k=='sub_min': feats['Total'] = v
        if 'sector' in entities:
            feats['sector'] = entities['sector']
        if 'tech' in qlow and 'sector' not in feats:
            feats['sector'] = 'Technology'
        elif 'pharma' in qlow:
            feats['sector'] = 'Pharma'
        elif 'manufactur' in qlow:
            feats['sector'] = 'Manufacturing'
        elif 'fmcg' in qlow or 'consumer' in qlow:
            feats['sector'] = 'FMCG/Consumer'
        elif 'energy' in qlow:
            feats['sector'] = 'Energy'
        elif 'bfsi' in qlow or 'bank' in qlow or 'finance' in qlow:
            feats['sector'] = 'BFSI'
        return {'tool': tool, 'args': {'target_features': feats, 'n': 5}}

    if tool == 'filter_search':
        VALID = {'gmp_min','gmp_max','qib_min','qib_max','sub_min','rii_min','hni_min','sector',
                 'listing_min','listing_max','ofs_max','ofs_min','size_max','size_min','year',
                 'price_max','price_min'}
        filters = {k:v for k,v in constraints.items() if k in VALID}
        if (('big ipo' in qlow or 'large ipo' in qlow or 'mega ipo' in qlow or 'large issue' in qlow)
            and 'size_min' not in filters):
            filters['size_min'] = 1000
        if 'small' in qlow and 'size_max' not in filters:
            filters['size_max'] = 500
        if 'tech' in qlow and 'sector' not in entities:
            filters['sector'] = 'Technology'
        if 'pharma' in qlow:
            filters['sector'] = 'Pharma/Healthcare'
        if 'fail' in qlow or 'flop' in qlow or 'lose' in qlow or 'bad' in qlow:
            filters['listing_max'] = 0
        if 'flat' in qlow:
            filters['listing_min'] = -2; filters['listing_max'] = 2
        if '100% ofs' in qlow or 'all ofs' in qlow:
            filters['ofs_min'] = 90 if 'ofs_max' not in filters else filters.get('ofs_max')
        if 'huge premium' in qlow or 'big gain' in qlow or 'most return' in qlow:
            filters['listing_min'] = 30
        if 'qib' in qlow and ("not subscrib" in qlow or "low qib" in qlow or "no qib" in qlow):
            filters['qib_max'] = 1
        if 'sector' in entities and 'sector' not in filters:
            filters['sector'] = entities['sector']
        return {'tool': tool, 'args': {'filters': filters}}

    if tool == 'pattern_analysis':
        if 'month' in qlow or constraints.get('group_by_month'):
            return {'tool': tool, 'args': {'group_by': 'month'}}
        if 'big' in qlow or 'small' in qlow or 'size' in qlow or 'large' in qlow:
            return {'tool': tool, 'args': {'group_by': 'size_bucket'}}
        if 'subscribe' in qlow or 'subscription' in qlow:
            return {'tool': tool, 'args': {'group_by': 'sub_bucket'}}
        if 'time' in qlow or 'year' in qlow or 'recent' in qlow or 'when' in qlow:
            return {'tool': tool, 'args': {'group_by': 'ipo_year'}}
        return {'tool': tool, 'args': {'group_by': 'sector'}}

    if tool == 'correlation':
        if ' pe' in qlow or 'p/e' in qlow or 'valuation' in qlow or 'expensive' in qlow:
            return {'tool': tool, 'args': {'feature1':'pre_issue_pe','feature2':'Listing Gain'}}
        if 'promoter' in qlow:
            return {'tool': tool, 'args': {'feature1':'promoter_holding_post_ipo','feature2':'Listing Gain'}}
        if 'roe' in qlow or 'profitab' in qlow:
            return {'tool': tool, 'args': {'feature1':'roe_ronw','feature2':'Listing Gain'}}
        if 'ofs' in qlow:
            return {'tool': tool, 'args': {'feature1':'ofs_pct','feature2':'Listing Gain'}}
        if 'hni' in qlow:
            return {'tool': tool, 'args': {'feature1':'HNI','feature2':'Listing Gain'}}
        if 'retail' in qlow or 'rii' in qlow:
            return {'tool': tool, 'args': {'feature1':'RII','feature2':'Listing Gain'}}
        if 'gmp' in qlow:
            return {'tool': tool, 'args': {'feature1':'gmp_closing_gain_pct','feature2':'Listing Gain'}}
        if 'qib' in qlow or 'institution' in qlow:
            return {'tool': tool, 'args': {'feature1':'QIB','feature2':'Listing Gain'}}
        if 'subscription' in qlow or 'subscrib' in qlow or 'demand' in qlow:
            return {'tool': tool, 'args': {'feature1':'Total','feature2':'Listing Gain'}}
        if 'size' in qlow or 'big' in qlow or 'small' in qlow or 'crore' in qlow:
            return {'tool': tool, 'args': {'feature1':'Issue_Size(crores)','feature2':'Listing Gain'}}
        return {'tool': tool, 'args': {'feature1':'QIB','feature2':'Listing Gain'}}

    if tool == 'signal_check':
        feats = {k.replace('_min',''):v for k,v in constraints.items()}
        if 'big' in qlow or 'large' in qlow:
            feats.setdefault('sub', 50); feats.setdefault('qib', 10)
        if '100% ofs' in qlow:
            feats['ofs_pct'] = 100
        if 'low gmp' in qlow or 'no gmp' in qlow:
            feats['gmp'] = 0
        if 'high gmp' in qlow:
            feats['gmp'] = 25
        return {'tool': tool, 'args': {'ipo_features': feats}}

    if tool == 'anomaly_detection':
        feats = {k.replace('_min',''):v for k,v in constraints.items()}
        return {'tool': tool, 'args': {'ipo_features': feats}}

    if tool == 'timeline_analysis':
        y1 = constraints.get('year1')
        y2 = constraints.get('year2')
        if y1 and y2:
            return {'tool': tool, 'args': {'year1': y1, 'year2': y2}}
        if constraints.get('year'):
            y = constraints['year']
            return {'tool': tool, 'args': {'year1': y-1, 'year2': y}}
        return {'tool': tool, 'args': {'year1': 2024, 'year2': 2025}}

    if tool == 'calibration_query':
        return {'tool': tool, 'args': {'target_wr': 90}}

    return {'tool': tool, 'args': {}}

def execute_action(action: Dict, state: AgentState):
    tool = action['tool']
    args = action.get('args', {})
    result = TOOLS[tool](**args)
    insight = _build_insight(tool, args, result)
    state.add(tool, args, result, insight)
    if tool == 'similarity_search' and result.get('neighbours'):
        state.confidence = min(1.0, state.confidence + 0.3)
    elif tool == 'filter_search' and result.get('n_matched',0) > 0:
        state.confidence = min(1.0, state.confidence + 0.25)
    elif tool == 'pattern_analysis' and result.get('rows'):
        state.confidence = min(1.0, state.confidence + 0.2)
    else:
        state.confidence = min(1.0, state.confidence + 0.1)

_FEAT_NICE = {
    'HNI': 'wealthy-investor (HNI) subscription', 'QIB': 'institutional (QIB) subscription',
    'RII': 'retail subscription', 'Total': 'overall subscription',
    'Listing Gain': 'day-one listing gain', 'gmp_closing_gain_pct': 'grey-market premium',
    'pre_issue_pe': 'P/E ratio', 'roe_ronw': 'return on equity',
    'Issue_Size(crores)': 'issue size', 'promoter_holding_post_ipo': 'promoter holding',
    'ofs_pct': 'offer-for-sale share', 'fresh_issue_pct': 'fresh-issue share',
}
def _feat(name): return _FEAT_NICE.get(name, str(name).replace('_', ' '))

_GROUP_NICE = {'sub_bucket': 'subscription level', 'size_bucket': 'issue size',
               'month': 'listing month', 'sector': 'sector'}
def _grp(name): return _GROUP_NICE.get(name, str(name).replace('_', ' '))


def _build_insight(tool: str, args: Dict, result: Dict) -> str:
    """Turn a tool's raw output into one plain-English sentence."""
    if tool == 'similarity_search':
        nb = result.get('neighbours', [])
        if not nb: return ""
        wr = result.get('win_rate', 0); avg = result.get('avg_listing', 0)
        return (f"The most similar past IPOs averaged {avg:+.0f}% on listing day, with "
                f"{wr:.0%} of them finishing above the offer price. The closest match, "
                f"{nb[0]['name']}, listed {nb[0]['listing_gain']:+.0f}%.")
    if tool == 'filter_search':
        n = result.get('n_matched', 0)
        if not n:
            return "No past IPOs in the dataset matched those conditions."
        wr = result.get('win_rate', 0); avg = result.get('avg_listing', 0)
        names = ', '.join(s['name'] for s in result.get('sample', [])[:3])
        ex = f" For example: {names}." if names else ""
        return (f"{n} past IPOs matched, {wr:.0%} listed above offer, averaging "
                f"{avg:+.0f}% on day one.{ex}")
    if tool == 'pattern_analysis':
        rows = result.get('rows', [])
        if not rows: return ""
        best, worst = rows[0], rows[-1]
        g = _grp(result['group_by'])
        if best['group'] == worst['group']:
            return (f"Grouped by {g}: {best['group']} listed above offer "
                    f"{best['win_rate']:.0%} of the time, averaging {best['avg_listing']:+.0f}%.")
        return (f"Grouped by {g}, the strongest were IPOs at {best['group']}, they listed "
                f"above offer {best['win_rate']:.0%} of the time, averaging {best['avg_listing']:+.0f}%. "
                f"The weakest, {worst['group']}, listed positive only {worst['win_rate']:.0%} of the time.")
    if tool == 'correlation':
        if 'error' in result: return ""
        c = result['correlation']; n = result['n_samples']
        f1, f2 = _feat(result['feature1']), _feat(result['feature2'])
        strength = {'strong': 'strong', 'moderate': 'moderate', 'weak': 'weak'}.get(result['strength'], result['strength'])
        if c >= 0.3:
            tail = "Higher one has generally lined up with a higher other."
            move = "tend to rise together"
        elif c <= -0.3:
            tail = "A higher one has generally lined up with a lower other."
            move = "tend to move in opposite directions"
        else:
            tail = "But the link is loose, so on its own it is not a reliable signal."
            move = "show only a faint relationship"
        return (f"Across {n} past IPOs, {f1} and {f2} {move} "
                f"({strength} link, correlation {c:+.2f}). {tail}")
    if tool == 'signal_check':
        cautions = result.get('caution', [])
        lean = "leans APPLY" if result.get('signal_1_likely_apply') else "leans SKIP"
        out = (f"For an IPO with these numbers, the apply/skip model {lean}, with roughly "
               f"{result.get('signal_4_allotment','?')}% allotment odds per application")
        if cautions:
            out += ". Watch out: " + "; ".join(str(x) for x in cautions)
        return out + "."
    if tool == 'anomaly_detection':
        return result.get('verdict', '')
    if tool == 'timeline_analysis':
        y1, y2 = result.get('year1'), result.get('year2')
        s1, s2 = result.get(y1), result.get(y2)
        if s1 and s2:
            return (f"In {y1}, {s1['count']} IPOs listed positive {s1['wr']:.0%} of the time "
                    f"(averaging {s1['avg_listing']:+.0f}%); in {y2}, {s2['count']} IPOs at "
                    f"{s2['wr']:.0%} (averaging {s2['avg_listing']:+.0f}%).")
        return ""
    if tool == 'calibration_query':
        if 'error' in result: return ""
        return (f"To aim for about {result['target']}% accuracy, the system uses the "
                f"{result['model']} model at threshold {result['threshold']}, historically right "
                f"{result['avg_wr']}% of the time across {result['avg_picks']} picks.")
    return ""

def _load_live():
    """Load the currently-tracked IPOs (qualified_ipos.json) from likely folders."""
    cands = [os.environ.get("IPO_LEDGER_DIR", ""), ".", os.path.dirname(DATASET or "")]
    for d in cands:
        fp = os.path.join(d or ".", "qualified_ipos.json")
        if os.path.exists(fp):
            try:
                with open(fp) as fh:
                    return json.load(fh)
            except Exception:
                pass
    return []

_GENERIC_NAME = {'ipo', 'ltd', 'limited', 'india', 'the', 'and', 'technologies', 'technology',
                 'solutions', 'industries', 'company', 'corporation', 'services', 'systems',
                 'enterprises'}

# Words that must never be treated as an IPO's distinctive name token, nor as an
# "unknown IPO" the user named. Prevents "all ipos" -> "All Time Plastics", etc.
_COMMON_WORDS = {
    'all', 'time', 'now', 'new', 'best', 'worst', 'safest', 'good', 'bad', 'top', 'high', 'low',
    'big', 'small', 'more', 'most', 'less', 'this', 'that', 'any', 'which', 'what', 'when', 'how',
    'why', 'who', 'will', 'should', 'can', 'could', 'would', 'does', 'did', 'are', 'was', 'the',
    'company', 'companies', 'stock', 'stocks', 'share', 'shares', 'listing', 'list', 'gain', 'gains',
    'profit', 'money', 'rich', 'safe', 'crash', 'risk', 'risky', 'scared', 'worried', 'lose', 'make',
    'apply', 'buy', 'invest', 'price', 'market', 'sector', 'subscription', 'demand', 'gmp', 'help',
    'everything', 'returns', 'return', 'history', 'ever', 'right', 'time', 'going', 'good',
    'gud', 'kya', 'hai', 'kaise', 'kamaye', 'paisa', 'kaunsa', 'accha', 'acha', 'sahi', 'hoga',
    'milega', 'kitna', 'kitni', 'konsa', 'konsi', 'batao', 'bata', 'chahiye', 'mujhe', 'mera',
}

def _key_token(name):
    for t in re.findall(r'[a-z0-9]+', str(name).lower()):
        if t not in _GENERIC_NAME and t not in _COMMON_WORDS and len(t) >= 3:
            return t
    return None

def _names_match(name, q):
    kt = _key_token(name)
    return kt is not None and re.search(r'\b' + re.escape(kt) + r'\b', q) is not None

def _match_named_ipo(question):
    """('live', rec) / ('archive', row) if the question names a tracked IPO;
    ('unknown', name) if it clearly names an IPO we don't have; else None."""
    q = question.lower()
    for rec in _load_live():
        if _names_match(rec.get('ipo_name', ''), q):
            return ('live', rec)
    try:
        _load()
        names = _data['df']['IPO_Name'].dropna().unique()
        for nm in names:
            if _names_match(nm, q):
                return ('archive', _data['df'][_data['df']['IPO_Name'] == nm].iloc[0])
    except Exception:
        pass
    for pat in [r'\b([a-z][a-z&]{3,})\s+ipo\b',
                r'how did ([a-z][a-z&]{3,}) (?:list|do|perform|go|fare)',
                r'(?:tell me about|how is|how was) ([a-z][a-z&]{3,})\b',
                r'\bis ([a-z][a-z&]{3,}) (?:a good|good|worth|overvalued|undervalued)\b']:
        m = re.search(pat, q)
        if m and m.group(1) not in _COMMON_WORDS:
            return ('unknown', m.group(1))
    return None

def _live_ipo_answer(rec, question):
    p = rec.get('prediction', {}) or {}
    inp = rec.get('inputs', {}) or {}
    name = _clean_name(rec.get('ipo_name', ''))
    q = question.lower()
    bits = []
    verdict = p.get('verdict')
    if verdict:
        bits.append(f"{name}: our current call is {verdict} ({p.get('confidence','?')}/100 confidence).")
    else:
        bits.append(f"{name} is in the tracker, but it has not been scored yet (subscription not out).")
    if any(k in q for k in ['pe', 'p/e', 'earning', 'valuation', 'expensive', 'cheap', 'price']):
        pe = inp.get('pe')
        if pe is not None:
            if pe < 0:
                bits.append(f"On valuation: it is loss-making (negative P/E, {pe:.0f}), so earnings do not "
                            f"support the price yet - the bull case rests on growth, not profits. "
                            f"In the dataset, P/E barely predicts listing pops, so this is not a deal-breaker on its own.")
            else:
                bits.append(f"On valuation: it is priced at about {pe:.0f}x earnings.")
    nums = []
    if inp.get('gmp') is not None: nums.append(f"GMP {inp['gmp']:+.0f}%")
    if inp.get('sub') is not None: nums.append(f"overall subscription {inp['sub']:.1f}x")
    if inp.get('qib') is not None: nums.append(f"QIB {inp['qib']:.1f}x")
    if inp.get('hni') is not None: nums.append(f"HNI {inp['hni']:.1f}x")
    if nums:
        bits.append("Latest numbers: " + ", ".join(nums) + ".")
    cases = rec.get('cases', {}) or {}
    if cases.get('pos'):
        bits.append("Biggest point for it: " + cases['pos'][0].get('head', '') + ".")
    if cases.get('neg'):
        bits.append("Biggest concern: " + cases['neg'][0].get('head', '') + ".")
    if rec.get('note'):
        bits.append(rec['note'])
    cav = _risk_caveat(question)
    if cav:
        bits.append(cav)
    return " ".join(b for b in bits if b)

def _archive_ipo_answer(row, question):
    name = _clean_name(row['IPO_Name'])
    q = question.lower()
    parts = [f"{name} is a past IPO in the dataset."]
    lg = row.get('Listing Gain')
    if pd.notna(lg):
        verb = "popped" if lg > 0 else "fell" if lg < 0 else "listed flat"
        parts.append(f"It {verb} {lg:+.0f}% on its first day." if verb != "listed flat"
                     else "It listed roughly flat.")
    if any(k in q for k in ['pe', 'p/e', 'earning', 'valuation', 'expensive', 'cheap', 'price', 'support']):
        pe = row.get('pre_issue_pe')
        if pd.notna(pe):
            if pe < 0:
                outcome = ("and it still listed up" if pd.notna(lg) and lg > 0
                           else "and it listed down" if pd.notna(lg) and lg < 0 else "")
                parts.append(f"On valuation: it was loss-making (negative P/E), so earnings did not "
                             f"support the asking price {outcome}. Across the dataset, P/E barely predicts "
                             f"listing pops, so a stretched P/E alone has not been a reliable red flag.")
            else:
                parts.append(f"On valuation: it was priced at about {pe:.0f}x earnings.")
    nums = []
    for col, label, unit in [('Total', 'overall subscription', 'x'), ('QIB', 'QIB', 'x'),
                             ('HNI', 'HNI', 'x'), ('gmp_closing_gain_pct', 'GMP', '%'),
                             ('pre_issue_pe', 'P/E', '')]:
        v = row.get(col)
        if pd.notna(v):
            nums.append(f"{label} {v:.0f}{unit}" if unit else f"{label} {v:.0f}")
    if nums:
        parts.append("Its numbers were: " + ", ".join(nums) + ".")
    cav = _risk_caveat(question)
    if cav:
        parts.append(cav)
    return " ".join(parts)

def _unknown_ipo_answer(name):
    live = [_clean_name(r.get('ipo_name', '')) for r in _load_live()]
    live = [n for n in live if n][:6]
    tracking = ("Right now I'm tracking: " + ", ".join(live) + ". ") if live else ""
    return (f"I don't have \"{name.title()}\" in my data - I only cover Indian mainboard IPOs that "
            f"are currently open or already in the 331-IPO history, and that one is in neither. "
            f"{tracking}Ask me about one of those, or ask a general pattern question like "
            f"\"do loss-making IPOs still list well\".")


_FEAT_SHORT = {
    'HNI': 'HNI demand', 'QIB': 'QIB demand', 'RII': 'retail demand',
    'Total': 'overall subscription', 'gmp_closing_gain_pct': 'GMP',
    'pre_issue_pe': 'a high P/E', 'roe_ronw': 'high ROE',
    'promoter_holding_post_ipo': 'high promoter holding',
}
def _feat_short(name): return _FEAT_SHORT.get(name, _feat(name))

def _unit_for(feat):
    if feat in ('QIB', 'HNI', 'RII', 'Total'): return 'x'
    if feat == 'gmp_closing_gain_pct': return '%'
    return ''

def _clean_name(n):
    n = str(n)
    for suf in (' IPO', ' Ltd.', ' Ltd', ' Limited', ' (Exports)'):
        n = n.replace(suf, '')
    return n.strip()

def _find_obs(state, tool):
    return next((o for o in state.observations if o.tool == tool), None)

def _verdict_line(state):
    """A direct yes/no opener for 'is X a good sign' style questions."""
    q = state.question.lower()
    yesno = any(p in q for p in ["good sign", "bad sign", "good or bad", "reliable", "trust",
                                 "is high", "is low", "should i", "does it matter", "worth",
                                 "green flag", "red flag", "predict", "help", "hurt", "safe",
                                 "matter", "a good", "indicator", "crash", "do high", "do low",
                                 "underperform", "outperform", "go up", "go down", "fall", "tank",
                                 "pop", "flop", "better", "worse"])
    if not yesno:
        return ""
    corr = _find_obs(state, 'correlation')
    if corr and 'correlation' in corr.result:
        c = corr.result['correlation']
        feat = corr.result['feature1'] if corr.result['feature1'] != 'Listing Gain' else corr.result['feature2']
        fs = _feat_short(feat)
        if c >= 0.4:   return f"Short answer: yes - strong {fs} has been one of the more reliable green flags."
        if c >= 0.25:  return f"Short answer: somewhat - {fs} leans positive, but it is not decisive on its own."
        if c <= -0.25: return f"Short answer: the opposite - more {fs} has tended to go with weaker listings."
        return f"Short answer: not really - {fs} barely tracks with how IPOs list, so on its own it is a weak signal."
    return ""

def _example_line(state):
    """Pull real, named IPOs to illustrate. Uses a sensible high/low band (not
    outliers). If the link is strong, contrasts a high-metric winner with a
    low-metric loser; if weak, shows that high-metric IPOs landed all over."""
    corr = _find_obs(state, 'correlation')
    if not (corr and 'feature1' in corr.result):
        return ""
    feat = corr.result['feature1'] if corr.result['feature1'] != 'Listing Gain' else corr.result['feature2']
    c = corr.result.get('correlation', 0)
    try:
        df = _data['df']
        if feat not in df.columns or 'Listing Gain' not in df.columns:
            return ""
        sub = df[['IPO_Name', feat, 'Listing Gain']].dropna()
        if len(sub) < 12:
            return ""
        q70, q95 = sub[feat].quantile(0.70), sub[feat].quantile(0.95)
        q05, q30 = sub[feat].quantile(0.05), sub[feat].quantile(0.30)
        hi_band = sub[(sub[feat] >= q70) & (sub[feat] <= q95)]
        lo_band = sub[(sub[feat] >= q05) & (sub[feat] <= q30)]
        if hi_band.empty or lo_band.empty:
            return ""
        u = _unit_for(feat)
        def fv(v): return f"{v:.0f}{u}" if u else f"{v:.0f}"
        metric = _feat_short(feat).replace('a ', '').replace('high ', '')

        if abs(c) >= 0.3:
            hi = hi_band.sort_values('Listing Gain', ascending=False).iloc[0]
            lo = lo_band.sort_values('Listing Gain', ascending=True).iloc[0]
            return (f"For example, {_clean_name(hi['IPO_Name'])} ({metric} ~{fv(hi[feat])}) listed "
                    f"{hi['Listing Gain']:+.0f}%, while {_clean_name(lo['IPO_Name'])} (~{fv(lo[feat])}) "
                    f"listed {lo['Listing Gain']:+.0f}%.")
        # weak link: show high-metric IPOs went both ways
        good = hi_band.sort_values('Listing Gain', ascending=False).iloc[0]
        bad = hi_band.sort_values('Listing Gain', ascending=True).iloc[0]
        if _clean_name(good['IPO_Name']) == _clean_name(bad['IPO_Name']):
            return ""
        return (f"For example, two high-{metric} IPOs went opposite ways: "
                f"{_clean_name(good['IPO_Name'])} listed {good['Listing Gain']:+.0f}% but "
                f"{_clean_name(bad['IPO_Name'])} listed {bad['Listing Gain']:+.0f}% - so {metric} "
                f"alone did not decide it.")
    except Exception:
        return ""

_IPO_VOCAB = {'ipo', 'ipos', 'listing', 'list', 'lists', 'listed', 'subscription', 'subscribed',
    'subscribe', 'gmp', 'allotment', 'allot', 'qib', 'hni', 'rii', 'retail', 'offer', 'promoter',
    'promoters', 'share', 'shares', 'stock', 'stocks', 'equity', 'issue', 'mainboard', 'sme', 'pe',
    'p/e', 'roe', 'valuation', 'demand', 'oversubscribed', 'grey', 'market', 'debut', 'premium',
    'anchor', 'lot', 'band', 'price', 'priced', 'company', 'companies', 'firm', 'apply', 'applying'}

_OFFTOPIC_SIGNALS = ['weather', 'bitcoin', 'crypto', 'ethereum', 'dogecoin', 'nft', 'football',
    'basketball', 'movie', 'film', 'recipe', 'cook', 'joke', 'president', 'prime minister',
    'capital of', 'translate', 'song', 'lyrics', 'make money', 'get rich', 'lose weight',
    'gold rate', 'gold price', 'fixed deposit', 'mutual fund', 'real estate', 'mortgage',
    'mileage', 'restaurant', 'horoscope', 'lottery number']

def _offtopic_answer(question):
    ql = question.lower()
    # tax is out of scope (depends on holding period) -> point to a CA
    if 'tax' in ql:
        return ("I don't cover tax on IPO gains - that depends on how long you hold and is best checked "
                "with a CA or the current income-tax rules. I focus on whether an IPO is worth applying to "
                "and how it might list.")
    # broader market / other investments -> redirect even though they share a word with IPOs
    if any(k in ql for k in ['sensex', 'nifty', 'stock market', 'share market', 'mutual fund',
                             'invest in stocks', 'buy stocks', 'invest in stock', 'buy stock',
                             'fixed deposit', 'which stock', 'best stock']):
        return ("I only cover Indian mainboard IPOs, not the broader stock market, indices, or other "
                "investments. Ask me about an IPO - like \"is Advit Jewels worth applying?\" - or what "
                "the 331-IPO history shows.")
    toks = set(re.findall(r'[a-z/]+', ql))
    if toks & _IPO_VOCAB:
        return None  # mentions an IPO/stock word -> on-topic, let it through
    hit = any(s in ql for s in _OFFTOPIC_SIGNALS)
    buy_other = bool(re.search(r'\b(buy|invest in|trade|trading)\b', ql)) and 'ipo' not in ql
    if hit or buy_other:
        return ("I only answer questions about Indian mainboard IPOs - GMP, subscription, valuations, "
                "which IPOs are worth applying to, and what the 331-IPO history says about listing gains. "
                "Ask me something along those lines and I'll dig into the data for you.")
    return None  # uncertain -> let concept/data handle it

_METRICS = [
    (['promoter holding', 'promoter', 'promoters'], 'promoter_holding_post_ipo', 'promoter holding'),
    (['hni', 'wealthy', 'high net'], 'HNI', 'HNI demand'),
    (['qib', 'institution', 'institutional'], 'QIB', 'QIB demand'),
    (['retail', 'rii'], 'RII', 'retail demand'),
    (['subscription', 'subscribed', 'oversubscribed', 'demand'], 'Total', 'overall subscription'),
    (['gmp', 'grey market'], 'gmp_closing_gain_pct', 'GMP'),
    (['pe', 'p/e', 'earning', 'valuation', 'expensive', 'cheap'], 'pre_issue_pe', 'P/E'),
    (['size', 'small', 'big', 'large'], 'Issue_Size(crores)', 'issue size'),
    (['roe', 'return on equity'], 'roe_ronw', 'ROE'),
    (['ofs', 'offer for sale', 'offer-for-sale', 'founders selling'], 'ofs_pct', 'offer-for-sale share'),
    (['fresh issue', 'fresh capital', 'new money'], 'fresh_issue_pct', 'fresh-issue share'),
]
_ANALYTIC_WORDS = ['good sign', 'bad sign', 'reliable', 'red flag', 'green flag', 'predict', 'matter',
    'crash', 'underperform', 'outperform', 'better', 'worse', 'good', 'bad', 'help', 'hurt', 'worth',
    'tank', 'pop', 'flop', 'indicator', 'sign', 'high', 'low', 'does', 'do ', 'strong', 'best', 'mean',
    'enough', 'too low', 'too high', 'worth it', 'safe']

def _detect_metric(question):
    ql = question.lower()
    for keys, col, label in _METRICS:
        if any(re.search(r'\b' + re.escape(k) + r'\b', ql) for k in keys):
            return col, label
    return None, None

def _example_for_col(col, c):
    try:
        df = _data['df']
        if col not in df.columns or 'Listing Gain' not in df.columns:
            return ""
        sub = df[['IPO_Name', col, 'Listing Gain']].dropna()
        if len(sub) < 12:
            return ""
        q70, q95 = sub[col].quantile(0.70), sub[col].quantile(0.95)
        q05, q30 = sub[col].quantile(0.05), sub[col].quantile(0.30)
        hi_band = sub[(sub[col] >= q70) & (sub[col] <= q95)]
        lo_band = sub[(sub[col] >= q05) & (sub[col] <= q30)]
        if hi_band.empty or lo_band.empty:
            return ""
        u = _unit_for(col)
        def fv(v): return f"{v:.0f}{u}" if u else f"{v:.0f}"
        m = _feat_short(col).replace('a ', '').replace('high ', '')
        if abs(c) >= 0.3:
            hi = hi_band.sort_values('Listing Gain', ascending=False).iloc[0]
            lo = lo_band.sort_values('Listing Gain', ascending=True).iloc[0]
            return (f"For example, {_clean_name(hi['IPO_Name'])} ({m} ~{fv(hi[col])}) listed {hi['Listing Gain']:+.0f}%, "
                    f"while {_clean_name(lo['IPO_Name'])} (~{fv(lo[col])}) listed {lo['Listing Gain']:+.0f}%.")
        good = hi_band.sort_values('Listing Gain', ascending=False).iloc[0]
        bad = hi_band.sort_values('Listing Gain', ascending=True).iloc[0]
        if _clean_name(good['IPO_Name']) == _clean_name(bad['IPO_Name']):
            return ""
        return (f"For example, two high-{m} IPOs went opposite ways: {_clean_name(good['IPO_Name'])} listed "
                f"{good['Listing Gain']:+.0f}% but {_clean_name(bad['IPO_Name'])} listed {bad['Listing Gain']:+.0f}% "
                f"- so {m} alone did not decide it.")
    except Exception:
        return ""

def _metric_analysis_answer(question):
    """Analytical question about a known metric -> verdict + quartile split + example."""
    col, label = _detect_metric(question)
    if not col:
        return None
    ql = question.lower()
    # "what is X" / "what does X mean" / "explain X" are definitions -> let concept answer
    definitional = (re.search(r"\b(what (is|are|does|do)|what'?s|explain|define|meaning)\b", ql)
                    and not any(w in ql for w in ['best', 'highest', 'better', 'worse', 'vs', 'versus',
                                                  'predict', 'reliable', 'matter', 'red flag', 'good or bad']))
    if definitional:
        return None
    if not any(a in ql for a in _ANALYTIC_WORDS):
        return None
    try:
        _load()
        df = _data['df']
        if col not in df.columns:
            return None
        sub = df[[col, 'Listing Gain']].dropna()
        if len(sub) < 24:
            return None
        c = float(sub[col].corr(sub['Listing Gain']))
        sub = sub.copy()
        sub['_q'] = pd.qcut(sub[col].rank(method='first'), 4, labels=['bottom', 'lo', 'hi', 'top'])
        top, bot = sub[sub['_q'] == 'top'], sub[sub['_q'] == 'bottom']
        twr, tavg = (top['Listing Gain'] > 0).mean(), top['Listing Gain'].mean()
        bwr, bavg = (bot['Listing Gain'] > 0).mean(), bot['Listing Gain'].mean()
        if c >= 0.4:    v = f"Short answer: yes - strong {label} has been one of the more reliable green flags."
        elif c >= 0.2:  v = f"Short answer: somewhat - more {label} leans positive, but it is not decisive on its own."
        elif c <= -0.2: v = f"Short answer: the opposite - more {label} has tended to go with weaker listings."
        else:           v = f"Short answer: not really - {label} barely tracks with how IPOs list, so on its own it is a weak signal."
        body = (f"Across {len(sub)} past IPOs, the top quarter by {label} listed above offer {twr:.0%} of the time "
                f"(averaging {tavg:+.0f}%), versus {bwr:.0%} (averaging {bavg:+.0f}%) for the bottom quarter "
                f"- correlation {c:+.2f}.")
        ex = _example_for_col(col, c)
        tail = "Still, treat it as one signal among several - GMP, QIB and overall demand all matter too - not a guarantee."
        return " ".join(x for x in [v, body, ex, tail] if x)
    except Exception:
        return None

def _vague_personal_answer(question):
    ql = question.lower()
    if 'how much' in ql and ('lot' in ql or 'money' in ql or 'invest' in ql or 'need' in ql):
        return ("It depends on the IPO: one lot = the lot size (number of shares) times the offer price. For "
                "Indian mainboard IPOs the regulator sets the minimum at roughly Rs 14,000-15,000 per "
                "application. Open a specific IPO on the site to see its exact lot cost.")
    if any(p in ql for p in ['good time', 'right time', 'now a good', 'time to invest', 'time for ipo',
                             'time to buy ipo']):
        live = _load_live()
        scored = [r for r in live if (r.get('prediction') or {}).get('verdict')]
        if scored:
            applies = [r for r in scored if r['prediction'].get('verdict') == 'APPLY']
            names = ", ".join(f"{_clean_name(r['ipo_name'])} ({r['prediction']['verdict']} "
                              f"{r['prediction'].get('confidence','?')}/100)" for r in (applies or scored)[:5])
            return ("I don't forecast market timing or where the market is headed - nobody does that "
                    f"reliably. What I can do is tell you whether a specific open IPO clears the bar. Right "
                    f"now my calls are: {names}.")
        return ("I don't forecast market timing. I look at specific IPOs one at a time - name one and I'll "
                "give you the call.")
    if any(p in ql for p in ['this ipo', 'should i apply', 'worth applying', 'best ipo', 'good ipo',
                             'which one', 'which ipo', 'apply now', 'apply to', 'aply', 'shud i',
                             'kaunsa ipo', 'konsa ipo', 'kaunsa acha', 'kaunsa accha', 'accha ipo',
                             'acha ipo', 'konsa accha', 'safest', 'safe ipo', 'kaunsa', 'konsa']):
        live = _load_live()
        scored = [r for r in live if (r.get('prediction') or {}).get('verdict')]
        if scored:
            applies = [r for r in scored if r['prediction'].get('verdict') == 'APPLY']
            pick = applies or scored
            names = ", ".join(f"{_clean_name(r['ipo_name'])} ({r['prediction']['verdict']} "
                              f"{r['prediction'].get('confidence','?')}/100)" for r in pick[:5])
            lead = ("Of the IPOs open right now, the model says APPLY to: " if applies
                    else "I can't tell which IPO you mean. Of the ones open right now, my calls are: ")
            return (f"{lead}{names}. Name one (like \"is Turtlemint a good buy\") and I'll break down its "
                    f"numbers and reasoning.")
        return ("I can't tell which IPO you mean from here. Name a specific IPO - like \"is Turtlemint a "
                "good buy\" - and I'll pull its call, numbers, and reasoning.")
    return None


def _help_text():
    return ("Ask me about Indian mainboard IPOs - for example: \"is Advit Jewels worth applying?\", "
            "\"what is GMP?\", \"do high-subscription IPOs list higher?\", or \"how did Tata Technologies "
            "list?\". I'll pull the answer from the 331-IPO history and the IPOs open right now.")

def _empty_or_gibberish(question):
    q = (question or "").strip()
    if len(q) < 2:
        return _help_text()
    ql = q.lower().strip(" ?.!,")
    if ql in {"help", "why", "how", "what", "info", "explain", "tell me everything", "what is this",
              "how does this work", "what can you do", "menu", "start", "hi", "hello", "hey",
              "tell me more", "more", "everything"}:
        return _help_text()
    words = re.findall(r'[a-z]{2,}', q.lower())
    if not words:  # "????", emojis, numbers only
        return _help_text()
    # single keysmash like "asdfghjkl" (one long word with no vowel pair)
    if len(words) == 1 and len(words[0]) >= 7 and not re.search(r'[aeiou].*[aeiou]', words[0]):
        return _help_text()
    return None

def _guarantee_answer(question):
    ql = question.lower()
    if any(p in ql for p in ['guarantee', 'guaranteed', '100% safe', '100 % safe', 'sure shot',
                             'sure profit', 'always make money', 'always makes money', 'become rich',
                             'get rich', 'exactly how much', 'exact profit', 'no risk', 'risk free',
                             'risk-free', 'always winners', 'always pop', 'never lose', 'always profit',
                             'paisa kaise', 'paise kaise', 'kaise kamaye', 'kaise kamau', 'paisa kamana',
                             'paise kamana', 'kitna profit', 'kitna milega', 'how much profit',
                             'hw mch profit', 'how much will i earn', 'how much can i make',
                             '10x', '100x', 'multibagger', 'will it 10x', 'multi bagger']):
        return ("No IPO is guaranteed - anyone promising that is wrong. In this dataset about 72% of IPOs "
                "listed above their offer price, which also means roughly 1 in 4 did not, and even strong-looking "
                "ones sometimes fall on debut. What the data does is shift the odds: IPOs with strong demand "
                "(grey-market premium, subscription) have listed positive far more often. I can give you the call "
                "and the odds for a specific IPO - but never a certainty. Treat every application as money you "
                "can afford to lose.")
    return None

def _emotional_answer(question):
    ql = question.lower()
    if any(p in ql for p in ['life savings', 'all my savings', 'lose my house', 'scared', 'afraid',
                             'worried', 'anxious', 'panic', 'will i lose money', 'am i going to lose',
                             'should i be scared', 'losing sleep', 'terrified', 'freaking out']):
        return ("First the honest part: never put money you can't afford to lose into an IPO - listings are "
                "uncertain, and even good-looking IPOs sometimes fall on debut. If you've already applied and "
                "you're anxious, an allotment is usually a small amount, and you get to decide whether to hold or "
                "exit on listing day. I can't predict your specific outcome, but I can show you the call and the "
                "odds for a specific IPO - name one and I'll break it down. For anything involving your savings, "
                "it's worth speaking with a SEBI-registered advisor too.")
    return None

def _risk_caveat(question):
    ql = question.lower()
    if any(p in ql for p in ['all my money', 'all my savings', 'life savings', 'everything i have',
                             'all in', 'entire savings']):
        return ("And please don't put all your money into one IPO - that kind of concentration is how people "
                "get hurt. Size it as money you can afford to lose.")
    if any(p in ql for p in ['definitely', 'guaranteed', '100% safe', 'for sure', 'sure shot',
                             'double my money', 'will it double', 'doubles', 'double', 'triple',
                             '10x', 'no risk', 'risk free', 'certain']):
        return "But nothing here is guaranteed - it's a probability, not a certainty."
    return None

def _ipo_oneliner(kind, obj):
    if kind == 'live':
        p = obj.get('prediction') or {}
        bits = []
        g = obj.get('gmp_pct')
        if g is not None:
            bits.append(f"GMP {g:+.0f}%")
        sub = (obj.get('subscription') or {}).get('total')
        if sub:
            bits.append(f"sub {sub:.0f}x")
        num = (", " + ", ".join(bits)) if bits else ""
        return f"{_clean_name(obj.get('ipo_name',''))} - {p.get('verdict','?')} ({p.get('confidence','?')}/100){num}"
    row = obj
    lg = row.get('Listing Gain')
    pe = row.get('pre_issue_pe')
    extra = f", P/E {pe:.0f}" if pe is not None and pe == pe else ""
    return f"{_clean_name(row.get('IPO_Name',''))} - listed {lg:+.0f}%{extra}"

def _comparison_answer(question):
    q = question.lower()
    if not any(w in q for w in [' vs ', ' versus ', 'compare', 'better than', ' or worse']):
        return None
    found, seen = [], set()
    for rec in _load_live():
        if _names_match(rec.get('ipo_name', ''), q):
            n = _clean_name(rec.get('ipo_name', ''))
            if n not in seen:
                seen.add(n); found.append(('live', rec))
    try:
        _load()
        for nm in _data['df']['IPO_Name'].dropna().unique():
            if _names_match(nm, q):
                n = _clean_name(nm)
                if n not in seen:
                    seen.add(n)
                    found.append(('archive', _data['df'][_data['df']['IPO_Name'] == nm].iloc[0]))
    except Exception:
        pass
    if len(found) < 2:
        return None
    found = found[:3]
    lines = [_ipo_oneliner(k, o) for k, o in found]
    # pick a winner: live APPLY > live higher confidence; archive higher listing
    def score(item):
        k, o = item
        if k == 'live':
            p = o.get('prediction') or {}
            return (2 if p.get('verdict') == 'APPLY' else 1, p.get('confidence') or 0)
        lg = o.get('Listing Gain')
        return (1, (lg or 0))
    best = max(found, key=score)
    bn = _clean_name(best[1].get('ipo_name') if best[0] == 'live' else best[1].get('IPO_Name'))
    body = " | ".join(lines)
    return (f"Here's how they stack up: {body}. On these numbers {bn} looks the stronger of the two, but "
            f"compare them on the demand signals that matter most - GMP and subscription - and remember a "
            f"preliminary call can change once bidding data comes in. Neither is a sure thing.")

def _aggregate_answer(question):
    ql = question.lower()
    # "what kind / what type of IPOs list highest" is about characteristics -> predictors, not a single record
    if any(p in ql for p in ['what kind', 'what type', 'what makes', 'what drives', 'which kind',
                             'kinds of', 'types of']):
        return None
    try:
        _load()
        df = _data['df']
        d = df.dropna(subset=['Listing Gain'])
        if 'how many' in ql and any(w in ql for w in ['negative', 'below', 'fell', 'loss', 'down', 'flat']):
            n, tot = int((d['Listing Gain'] < 0).sum()), len(d)
            return (f"Of {tot} IPOs in the dataset, {n} ({n/tot:.0%}) listed below their offer price on day one. "
                    f"The other {tot-n} ({(tot-n)/tot:.0%}) listed at or above offer.")
        if 'how many' in ql and any(w in ql for w in ['positive', 'above', 'gain', 'profit', 'up']):
            n, tot = int((d['Listing Gain'] > 0).sum()), len(d)
            return f"Of {tot} IPOs in the dataset, {n} ({n/tot:.0%}) listed above their offer price on day one."
        if any(w in ql for w in ['highest', 'most', 'biggest', 'best']) and \
           any(w in ql for w in ['gain', 'return', 'list', 'pop', 'ever', 'jump', 'rise']):
            t = d.sort_values('Listing Gain', ascending=False).iloc[0]
            return (f"The biggest first-day pop in the dataset was {_clean_name(t['IPO_Name'])}, up "
                    f"{t['Listing Gain']:+.0f}% on debut. Huge pops like that are rare and usually came with very "
                    f"heavy subscription and a high grey-market premium.")
        if any(w in ql for w in ['worst', 'lowest', 'biggest loss', 'biggest drop']) and \
           any(w in ql for w in ['gain', 'return', 'list', 'ipo', 'ever', 'history', 'fall', 'drop', 'crash']):
            b = d.sort_values('Listing Gain').iloc[0]
            return (f"The worst first-day drop in the dataset was {_clean_name(b['IPO_Name'])}, down "
                    f"{abs(b['Listing Gain']):.0f}% on debut.")
        if 'average' in ql and 'gmp' in ql and 'gmp_closing_gain_pct' in df.columns:
            s = df['gmp_closing_gain_pct'].dropna()
            return (f"Across the dataset the average grey-market premium was about {s.mean():+.0f}% "
                    f"(median {s.median():+.0f}%) - but GMP varies wildly by IPO, so the average alone isn't "
                    f"something to act on.")
        if 'average' in ql and any(w in ql for w in ['listing', 'gain', 'return', 'pop']):
            return (f"The average day-one listing gain across {len(d)} IPOs was {d['Listing Gain'].mean():+.0f}% "
                    f"(median {d['Listing Gain'].median():+.0f}%), with about {(d['Listing Gain']>0).mean():.0%} "
                    f"listing above offer.")
    except Exception:
        pass
    return None


def _lossmaking_answer(question):
    ql = question.lower()
    if not any(k in ql for k in ['loss making', 'loss-making', 'lossmaking', 'unprofitable',
                                 'not profitable', 'no profit', 'losing money']):
        return None
    try:
        _load()
        df = _data['df']
        s = df[['pre_issue_pe', 'Listing Gain']].dropna()
        loss, prof = s[s['pre_issue_pe'] < 0], s[s['pre_issue_pe'] > 0]
        if len(loss) < 3:
            return ("There are very few loss-making companies in this dataset - most Indian mainboard IPOs "
                    "come to market profitable - so I can't draw a firm data rule. In general, profitability "
                    "matters far less than demand: listing gains are driven mostly by grey-market premium and "
                    "subscription, not by whether the company is yet profitable. A loss-making company with strong "
                    "demand has often listed well, while a profitable one with weak demand has flopped.")
        lwr, lavg = (loss['Listing Gain'] > 0).mean(), loss['Listing Gain'].mean()
        pwr, pavg = (prof['Listing Gain'] > 0).mean(), prof['Listing Gain'].mean()
        small = f" (only {len(loss)} in the data, so treat this loosely)" if len(loss) < 10 else ""
        verdict = ("Short answer: not really - being loss-making has not doomed IPOs."
                   if lwr >= 0.55 else
                   "Short answer: there is some truth to it, but it is far from a rule.")
        return (f"{verdict} Of {len(loss)} loss-making IPOs{small}, {lwr:.0%} still listed above offer "
                f"(averaging {lavg:+.0f}%), versus {pwr:.0%} (averaging {pavg:+.0f}%) for the {len(prof)} "
                f"profitable ones. Profitability matters far less than demand: a loss-making company with strong "
                f"subscription and grey-market premium has often listed well, while a profitable one with weak "
                f"demand has flopped.")
    except Exception:
        return None


def _sector_answer(question):
    ql = question.lower()
    if 'sector' not in ql:
        return None
    if not any(w in ql for w in ['best', 'worst', 'matter', 'which', 'good', 'bad', 'perform',
                                 'highest', 'strongest', 'weakest', 'top', 'better']):
        return None
    try:
        _load()
        df = _data['df']
        if 'sector' not in df.columns:
            return None
        d = df.dropna(subset=['Listing Gain', 'sector'])
        g = d.groupby('sector')['Listing Gain'].agg(avg='mean', wr=lambda x: (x > 0).mean(), n='count')
        g = g[g['n'] >= 5].sort_values('wr', ascending=False)
        if g.empty:
            return None
        best, worst = g.iloc[0], g.iloc[-1]
        return (f"Sector does shift the odds, though it is a weaker signal than demand. The strongest has been "
                f"{g.index[0]} ({best['wr']:.0%} listed above offer, averaging {best['avg']:+.0f}% across "
                f"{int(best['n'])} IPOs); the weakest was {g.index[-1]} ({worst['wr']:.0%}, averaging "
                f"{worst['avg']:+.0f}%). Most sectors land in between, so sector alone won't decide it - "
                f"grey-market premium and subscription matter more.")
    except Exception:
        return None


def _predictors_answer(question):
    ql = question.lower()
    trig = (any(p in ql for p in ['what kind', 'what type', 'which ipo', 'what makes', 'what predicts',
                                  'what drives', 'kinds of ipo', 'types of ipo'])
            and any(w in ql for w in ['list', 'perform', 'pop', 'best', 'highest', 'well', 'do',
                                      'gain', 'strong', 'succeed', 'win']))
    if not trig:
        return None
    try:
        _load()
        df = _data['df']
        cands = [('grey-market premium', 'gmp_closing_gain_pct'), ('overall subscription', 'Total'),
                 ('HNI demand', 'HNI'), ('QIB demand', 'QIB')]
        scored = []
        for label, col in cands:
            if col in df.columns:
                s = df[[col, 'Listing Gain']].dropna()
                if len(s) > 30:
                    scored.append((label, float(s[col].corr(s['Listing Gain']))))
        if not scored:
            return None
        scored.sort(key=lambda x: -x[1])
        top = ", ".join(f"{l} (correlation {c:+.2f})" for l, c in scored[:3])
        return (f"The strongest pull on day-one gains comes from demand signals: {top}. In plain terms, IPOs "
                f"that arrive with a high grey-market premium and heavy subscription - especially from "
                f"institutions and wealthy investors - have tended to list highest. Valuation (P/E) and "
                f"issue size matter far less.")
    except Exception:
        return None


def synthesize(state: AgentState) -> str:
    """Direct verdict + plain-English evidence + a real example + an honest caveat."""
    obs = state.observations
    evidence, seen = [], set()
    for o in obs:
        s = (o.insight or "").strip()
        if s and not s.startswith("No past IPOs") and s not in seen:
            seen.add(s); evidence.append(s)

    if not evidence:
        return ("I could not find a clear pattern for that in the dataset. Try asking about a "
                "specific metric (\"what is GMP\"), a sector, or a comparison like \"do high P/E "
                "IPOs do worse\".")

    parts = []
    verdict = _verdict_line(state)
    if verdict:
        parts.append(verdict)
    parts.append(" ".join(evidence))
    example = _example_line(state)
    if example:
        parts.append(example)

    caveats = [str(c).replace("WR", "win rate") for c in state.contradictions]
    if caveats:
        parts.append("One caveat: " + "; ".join(caveats) + ".")
    elif verdict:
        parts.append("Still, treat it as one signal among several - GMP, QIB and overall demand "
                     "all matter too - not a guarantee on its own.")
    return " ".join(parts)

def critique(state: AgentState, answer: str) -> Dict:
    """Does the answer actually address the question?"""
    q = state.question.lower()
    issues = []
    if 'why' in q and 'because' not in answer.lower() and 'due to' not in answer.lower():
        issues.append("Question asked 'why' but answer doesn't explain cause")
    if state.confidence < 0.4:
        issues.append("Low confidence, limited evidence gathered")
    if len(state.observations) < 2:
        issues.append("Only used 1 tool, may have missed angles")
    return {'issues': issues, 'addresses_question': len(issues) == 0}

class ResearchAgent:
    MAX_STEPS = 15

    def run(self, question: str, verbose: bool = True) -> Dict:
        state = AgentState(question=question)

        if verbose:
            print(f"\n{'='*65}")
            print(f"RESEARCH AGENT")
            print(f"Question: {question}")
            print(f"{'='*65}")

        # 0) Empty / gibberish -> a friendly nudge, not 331-stat mush.
        gib = _empty_or_gibberish(question)
        if gib:
            return {"question": question, "parser": "help", "intent": "help",
                    "answer": gib, "insights": []}

        # 1) Comparison of two named IPOs ("X vs Y", "compare X and Y").
        comp = _comparison_answer(question)
        if comp:
            if verbose: print("\n[COMPARE] Two named IPOs")
            return {"question": question, "parser": "comparison", "intent": "compare",
                    "answer": comp, "insights": []}

        # 2) A specific named IPO? Live call, past listing, or honest "not tracked".
        try:
            named = _match_named_ipo(question)
        except Exception:
            named = None
        if named:
            kind, obj = named
            ans = (_live_ipo_answer(obj, question) if kind == 'live'
                   else _archive_ipo_answer(obj, question) if kind == 'archive'
                   else _unknown_ipo_answer(obj))
            if ans:
                if verbose: print(f"\n[NAMED] Matched a {kind} IPO")
                return {"question": question, "parser": "named_ipo", "intent": "specific_ipo",
                        "answer": ans, "insights": []}

        # 3) Clearly off-topic (no IPO/stock words at all)?
        off = _offtopic_answer(question)
        if off:
            return {"question": question, "parser": "offtopic", "intent": "redirect",
                    "answer": off, "insights": []}

        # 4) "Guarantee me profit" / "100% safe" / "always make money" -> honest no.
        guar = _guarantee_answer(question)
        if guar:
            return {"question": question, "parser": "guidance", "intent": "no_guarantee",
                    "answer": guar, "insights": []}

        # 5) Fear / "life savings" / "will I lose money" -> empathetic + honest.
        emo = _emotional_answer(question)
        if emo:
            return {"question": question, "parser": "guidance", "intent": "reassurance",
                    "answer": emo, "insights": []}

        # 6) Aggregate / superlative ("how many listed negative", "highest gain ever").
        agg = _aggregate_answer(question)
        if agg:
            return {"question": question, "parser": "aggregate", "intent": "analysis",
                    "answer": agg, "insights": []}

        # 3) Vague "should I apply / which IPO / how much" without naming one?
        vague = _vague_personal_answer(question)
        if vague:
            return {"question": question, "parser": "guidance", "intent": "vague",
                    "answer": vague, "insights": []}

        # 4) Analytical question about a known metric -> verdict + numbers + example.
        loss = _lossmaking_answer(question)
        if loss:
            return {"question": question, "parser": "metric", "intent": "analysis",
                    "answer": loss, "insights": []}
        metric = _metric_analysis_answer(question)
        if metric:
            if verbose: print("\n[METRIC] Direct metric analysis")
            return {"question": question, "parser": "metric", "intent": "analysis",
                    "answer": metric, "insights": []}

        # 5) "What kind of IPOs list highest" -> the top predictors.
        sector = _sector_answer(question)
        if sector:
            return {"question": question, "parser": "sector", "intent": "analysis",
                    "answer": sector, "insights": []}
        pred = _predictors_answer(question)
        if pred:
            return {"question": question, "parser": "predictors", "intent": "analysis",
                    "answer": pred, "insights": []}

        # 6) Pure definition ("what is GMP")?
        try:
            from concept_explainer import find_concept
            concept_answer = find_concept(question)
        except Exception:
            concept_answer = None
        if concept_answer:
            if verbose: print("\n[CONCEPT] Answered from the glossary")
            return {"question": question, "parser": "concept", "intent": "definition",
                    "answer": concept_answer, "insights": []}

        # 7) Otherwise, run the full data pipeline.
        state.parsed_intent = parse_question(question)
        if verbose:
            print(f"\n[PARSE] Intent: {state.parsed_intent['type']}")
            if state.parsed_intent['constraints']:
                print(f"    Constraints: {state.parsed_intent['constraints']}")
            if state.parsed_intent['entities']:
                print(f"    Entities: {state.parsed_intent['entities']}")

        state.plan = plan_initial(state.parsed_intent)
        if verbose:
            print(f"\n[PLAN]  Initial steps: {state.plan}")

        for step in range(self.MAX_STEPS):
            action = reflect_and_decide(state)
            if action is None:
                if verbose: print(f"\n[STOP]  Agent decided to finish")
                break
            if verbose:
                print(f"\n[STEP {step+1}] Decided: {action['tool']}")
            try:
                execute_action(action, state)
                if verbose:
                    print(f"     -> {state.observations[-1].insight}")
                    print(f"     (confidence: {state.confidence:.0%})")
            except Exception as e:
                if verbose: print(f"     [X] Tool error: {e}")
                state.contradictions.append(f"{action['tool']} failed: {e}")

        if verbose: print(f"\n[SYNTH] Building final answer...")
        answer = synthesize(state)
        state.final_answer = answer

        critique_result = critique(state, answer)
        if verbose and critique_result['issues']:
            print(f"\n[CRITIQUE] Issues found:")
            for i in critique_result['issues']: print(f" - {i}")

        if verbose:
            print(f"\n{'-'*65}")
            print(answer)
            print(f"\n{'-'*65}")
            print(f"Steps: {state.step_count} | Tools used: {state.tools_used()} | "
                  f"Confidence: {state.confidence:.0%}")

        return {
            'question':    question,
            'intent':      state.parsed_intent,
            'plan':        state.plan,
            'steps_taken': state.step_count,
            'tools_used':  list(state.tools_used()),
            'confidence':  state.confidence,
            'observations':[{
                'step':o.step,'tool':o.tool,'insight':o.insight
            } for o in state.observations],
            'contradictions': state.contradictions,
            'answer':       answer,
            'final_answer': answer,
            'critique':    critique_result,
        }

TEST_QUERIES = [
    "Find IPOs with GMP above 30 and QIB above 50",
    "Which sectors perform best?",
    "Find IPOs similar to high GMP manufacturing",
    "What's the correlation between QIB and listing gain?",
    "Why do BFSI IPOs underperform?",
]

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        agent = ResearchAgent()
        for q in TEST_QUERIES:
            agent.run(q)
            print("\n" + "="*65 + "\n")
    elif len(sys.argv) > 1:
        agent = ResearchAgent()
        agent.run(' '.join(sys.argv[1:]))
    else:
        print(__doc__)
