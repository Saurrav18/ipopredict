"""Scoring module: 4-model ensemble extracted from the pipeline, import-safe.
No auto-install, no selenium, just the model training and per-IPO scoring."""
import os, warnings
import pandas as pd, numpy as np
from sklearn.ensemble import RandomForestClassifier, ExtraTreesClassifier
from sklearn.preprocessing import RobustScaler, LabelEncoder
from sklearn.neighbors import NearestNeighbors
from sklearn.decomposition import PCA
from xgboost import XGBRegressor, XGBClassifier
from lightgbm import LGBMClassifier
warnings.filterwarnings('ignore')
def log(*a, **k): print(*a)


# The four win-models for the tier ladder. The calibration agent picks, per tier,
# which of these + threshold best hits the target win rate. Kept here so the
# calibration and the live app train the SAME models on the SAME features.
def win_models():
    return {
        'RandomForest': RandomForestClassifier(n_estimators=200, max_depth=10,
                            min_samples_leaf=3, random_state=42, n_jobs=-1),
        'XGBoost':      XGBClassifier(n_estimators=150, max_depth=4, learning_rate=0.05,
                            subsample=0.8, random_state=42, eval_metric='logloss', verbosity=0),
        'LightGBM':     LGBMClassifier(n_estimators=150, max_depth=4, learning_rate=0.05,
                            num_leaves=31, random_state=42, verbose=-1),
        'ExtraTrees':   ExtraTreesClassifier(n_estimators=200, max_depth=10,
                            random_state=42, n_jobs=-1),
    }

def prep_frame(df):
    """Shared data prep: derived columns (moving win-rates, sector stats, gmp
    accuracy, ...). Used by BOTH the live app and the calibration so their
    feature spaces are identical."""
    df = df.dropna(subset=['Listing Gain','gmp_closing_gain_pct']).copy()
    df['Date'] = pd.to_datetime(df['Date'], dayfirst=True)
    df = df.sort_values('Date').reset_index(drop=True)
    df['win'] = (df['Listing Gain']>0).astype(int)
    df['big_win'] = (df['Listing Gain']>20).astype(int)
    df['sector_code'] = LabelEncoder().fit_transform(df['sector'].fillna('Unknown'))
    df['mwr5']  = df['win'].shift(1).rolling(5,min_periods=3).mean().fillna(0.7)
    df['mwr10'] = df['win'].shift(1).rolling(10,min_periods=5).mean().fillna(0.7)
    df['al10']  = df['Listing Gain'].shift(1).rolling(10,min_periods=5).mean().fillna(0)
    df['ag10']  = df['gmp_closing_gain_pct'].shift(1).rolling(10,min_periods=5).mean().fillna(0)
    df['gpr']   = df['gmp_closing_day']/(df['Offer Price']+1)
    df['ipc']   = [int(((df['Date']<df.loc[i,'Date']) &
                   (df['Date']>=df.loc[i,'Date']-pd.Timedelta(30))).sum()) for i in df.index]
    for c in ['sector_wr','sector_avg_list','sector_vol','sector_bias','gmp_acc','mvol','grank','aprx']:
        df[c] = np.nan
    for i in df.index:
        past=df[df.index<i]; ps=past[past['sector']==df.loc[i,'sector']].tail(15)
        p10=past.tail(10); p20=past.tail(20)
        if len(ps)>=3:
            df.loc[i,'sector_wr']=ps['win'].mean()
            df.loc[i,'sector_avg_list']=ps['Listing Gain'].mean()
            df.loc[i,'sector_vol']=ps['Listing Gain'].std()
            df.loc[i,'sector_bias']=(ps['Listing Gain']-ps['gmp_closing_gain_pct']).mean()
        if len(p10)>=5:
            df.loc[i,'gmp_acc']=(p10['Listing Gain']-p10['gmp_closing_gain_pct']).abs().mean()
            df.loc[i,'mvol']=p10['Listing Gain'].std()
        if len(p20)>=10:
            df.loc[i,'grank']=(p20['gmp_closing_gain_pct']<df.loc[i,'gmp_closing_gain_pct']).mean()
        df.loc[i,'aprx']=1/(np.log1p(df.loc[i,'Total'])*np.log1p(df.loc[i,'Issue_Size(crores)'])+1)
    for c in ['sector_wr','sector_avg_list','sector_vol','sector_bias','gmp_acc','mvol','grank','aprx']:
        df[c]=df[c].fillna(df[c].median() if df[c].notna().any() else 0)
    df['aprx']=df['aprx']/df['aprx'].max()
    return df

# Columns the tier win-models must NOT see: live subscription builds up over the
# bidding window, so feeding a half-built number depresses every early prediction.
# The tier is therefore computed from GMP + fundamentals + sector/market history,
# which are stable from day one. (Subscription still feeds the range/big-win models.)
_SUB_COLS = {'Total','QIB','HNI','RII','lt','lq','sq','id','gts','gqs','gis','s6','aprx'}

def make_win_features(d):
    """Subscription-free feature set for the tier win-models."""
    X = make_features(d)
    return X.drop(columns=[c for c in _SUB_COLS if c in X.columns])


def build_models(dataset_path):
    """Train all ML models on full dataset. Returns model bundle."""
    log("Loading dataset and training models...")
    df=prep_frame(pd.read_excel(dataset_path))
    N=len(df)
    le=LabelEncoder().fit(df['sector'].fillna('Unknown'))

    Xall=make_features(df)
    yl=df['Listing Gain'].values; yw=df['win'].values; yb=df['big_win'].values
    sc=RobustScaler(); Xtr=sc.fit_transform(Xall.values)

    # --- tier win-models: subscription-free, all four, each with its own scaler,
    # so the live app can score whichever model the calibration chose per tier ---
    Xwin=make_win_features(df)
    scw=RobustScaler(); Xwtr=scw.fit_transform(Xwin.values)
    winm={}
    for nm, mdl in win_models().items():
        mdl.fit(Xwtr, yw); winm[nm]=mdl

    rf=RandomForestClassifier(n_estimators=300,max_depth=10,
                               min_samples_leaf=3,random_state=42,n_jobs=-1)
    rf.fit(Xtr,yw)

    CAL_W = 40
    tr_q = list(range(N-CAL_W))
    cal_q = list(range(N-CAL_W, N))
    Xtr_q = sc.transform(Xall.iloc[tr_q].values)
    gq={q:XGBRegressor(n_estimators=200,max_depth=4,learning_rate=0.05,
                        subsample=0.8,colsample_bytree=0.7,
                        objective='reg:quantileerror',quantile_alpha=q,
                        random_state=42,verbosity=0) for q in [0.03,0.50,0.90]}
    [gq[q].fit(Xtr_q, yl[tr_q]) for q in [0.03,0.50,0.90]]

    xgb_big=XGBClassifier(n_estimators=200,max_depth=4,learning_rate=0.05,
                           subsample=0.8,random_state=42,
                           eval_metric='logloss',verbosity=0)
    xgb_big.fit(Xtr,yb)

    Xcal_q = sc.transform(Xall.iloc[cal_q].values)
    lo_c = gq[0.03].predict(Xcal_q)
    hi_c = gq[0.90].predict(Xcal_q)
    mid_c = gq[0.50].predict(Xcal_q)
    buf_lo = float(np.quantile(np.maximum(lo_c - yl[cal_q], 0), 0.80))
    buf_hi = float(np.quantile(np.maximum(yl[cal_q] - hi_c, 0), 0.80))
    bias = float(np.nanmean(yl[cal_q] - mid_c))

    RAG_F=['gmp_closing_gain_pct','Total','QIB','RII','Issue_Size(crores)',
           'Offer Price','pre_issue_pe','roe_ronw','ofs_pct',
           'promoter_holding_post_ipo','sector_code']
    rdf=df[RAG_F].fillna(df[RAG_F].median())
    sr=RobustScaler(); Xr=sr.fit_transform(rdf)
    knn=NearestNeighbors(n_neighbors=6,metric='cosine',algorithm='brute')
    knn.fit(Xr)

    PCA_F=['gmp_closing_gain_pct','Total','QIB','RII','Issue_Size(crores)','sector_code']
    pdf=df[PCA_F].fillna(df[PCA_F].median())
    sp=RobustScaler(); Xp=sp.fit_transform(pdf)
    pca_m=PCA(n_components=4,random_state=42); pca_m.fit(Xp)
    all_pca=np.array([float(np.mean((pca_m.inverse_transform(
        pca_m.transform([row]))-[row])**2)) for row in Xp])
    pca_thr=float(np.percentile(all_pca,92))

    sector_stats={}
    for sec in df['sector'].dropna().unique():
        sub=df[df['sector']==sec]
        if len(sub)>0:
            sector_stats[sec]={
                'sector_wr':float(sub['sector_wr'].iloc[-1]),
                'sector_avg_list':float(sub['sector_avg_list'].iloc[-1]),
                'sector_vol':float(sub['sector_vol'].iloc[-1]),
                'sector_bias':float(sub['sector_bias'].iloc[-1]),
                'recent_wr':float(sub.tail(5)['win'].mean()),
                'all_wr':float(sub['win'].mean()),
            }

    latest=df.iloc[-1]
    log(f"Models trained on {N} IPOs")
    return {
        'rf':rf,'gq':gq,'xgb_big':xgb_big,'sc':sc,
        'winm':winm,'scw':scw,
        'buf_lo':buf_lo,'buf_hi':buf_hi,'bias':bias,
        'knn':knn,'sr':sr,'RAG_F':RAG_F,
        'pca_m':pca_m,'sp':sp,'PCA_F':PCA_F,'pca_thr':pca_thr,
        'all_pca':all_pca,
        'sector_stats':sector_stats,
        'latest':latest,
        'df':df,'le':le,'yl':yl,
        'aprx_max':float(df['aprx'].max()),
    }


def make_features(d):
    X=pd.DataFrame(index=d.index)
    for c in ['gmp_closing_gain_pct','gmp_closing_day','Total','QIB','HNI','RII',
              'Issue_Size(crores)','market_sentiment_ratio','ratio_1m_vs_3m',
              'nifty_volatility_30d','trend_score','Offer Price']:
        X[c]=d[c] if c in d.columns else 0
    X['ofs_pct']=d['ofs_pct'].fillna(50) if 'ofs_pct' in d.columns else 50
    X['fresh_issue_pct']=d['fresh_issue_pct'].fillna(50) if 'fresh_issue_pct' in d.columns else 50
    X['promoter_holding_post_ipo']=d['promoter_holding_post_ipo'].fillna(60) if 'promoter_holding_post_ipo' in d.columns else 60
    X['pre_issue_pe']=d['pre_issue_pe'].fillna(25) if 'pre_issue_pe' in d.columns else 25
    X['roe_ronw']=d['roe_ronw'].fillna(15) if 'roe_ronw' in d.columns else 15
    X['li']=np.log1p(X['Issue_Size(crores)']); X['lt']=np.log1p(X['Total'])
    X['lq']=np.log1p(X['QIB'])
    X['sq']=X['QIB']/(X['Total']+1); X['id']=(X['QIB']+X['HNI'])/(X['Total']+1)
    X['g2']=X['gmp_closing_gain_pct']**2
    X['gs']=np.sign(X['gmp_closing_gain_pct'])*np.abs(X['gmp_closing_gain_pct'])**.5
    X['gts']=X['gmp_closing_gain_pct']*X['lt']
    X['gqs']=X['gmp_closing_gain_pct']*X['lq']
    X['gms']=X['gmp_closing_gain_pct']*X['market_sentiment_ratio']
    X['gis']=X['gmp_closing_gain_pct']*X['id']
    X['fo']=X['fresh_issue_pct']/(X['ofs_pct']+1)
    X['vg']=X['gmp_closing_gain_pct']/(X['nifty_volatility_30d']+1)
    X['rp']=X['roe_ronw']/(X['pre_issue_pe']+1)
    X['s6']=(X['Total']>=60).astype(int)
    for c in ['sector_wr','sector_avg_list','sector_vol','sector_bias',
              'aprx','mwr5','mwr10','mvol','ipc','gmp_acc','gpr','grank']:
        X[c]=d[c] if c in d.columns else 0
    X['al10']=d['al10'] if 'al10' in d.columns else 0
    X['gva']=d['gmp_closing_gain_pct']-d['ag10'] if 'ag10' in d.columns else 0
    X['wg']=d['mwr10']*d['gmp_closing_gain_pct'] if 'mwr10' in d.columns else 0
    X['hm']=(d['mwr5']>=0.8).astype(float) if 'mwr5' in d.columns else 0
    X['gadj']=d['gmp_closing_gain_pct']+d['sector_bias'] if 'sector_bias' in d.columns else d['gmp_closing_gain_pct']
    return X.fillna(0)


def score_ipo(row, models):
    """Score one IPO row against all signals."""
    m=models; le=m['le']; latest=m['latest']; ss=m['sector_stats']
    sector=str(row.get('sector','Unknown'))
    try: sec_code=int(le.transform([sector])[0])
    except: sec_code=0
    sec=ss.get(sector,{})

    feat_row=pd.DataFrame([{
        'gmp_closing_gain_pct':row.get('gmp_closing_gain_pct',0),
        'gmp_closing_day':row.get('gmp_closing_day',0),
        'Total':row.get('Total',1),'QIB':row.get('QIB',0),
        'HNI':row.get('HNI',0),'RII':row.get('RII',1),
        'Issue_Size(crores)':row.get('Issue_Size(crores)',100),
        'market_sentiment_ratio':row.get('market_sentiment_ratio',float(latest['market_sentiment_ratio'])),
        'ratio_1m_vs_3m':row.get('ratio_1m_vs_3m',float(latest['ratio_1m_vs_3m'])),
        'nifty_volatility_30d':row.get('nifty_volatility_30d',float(latest['nifty_volatility_30d'])),
        'trend_score':row.get('trend_score',int(latest['trend_score'])),
        'ofs_pct':row.get('ofs_pct',50),'fresh_issue_pct':row.get('fresh_issue_pct',50),
        'promoter_holding_post_ipo':row.get('promoter_holding_post_ipo',60),
        'pre_issue_pe':row.get('pre_issue_pe',25),'roe_ronw':row.get('roe_ronw',15),
        'Offer Price':row.get('Offer Price',100),
        'sector_wr':sec.get('sector_wr',float(latest['sector_wr'])),
        'sector_avg_list':sec.get('sector_avg_list',float(latest['sector_avg_list'])),
        'sector_vol':sec.get('sector_vol',float(latest['sector_vol'])),
        'sector_bias':sec.get('sector_bias',float(latest['sector_bias'])),
        'aprx':1/(np.log1p(max(row.get('Total',1),0.01))*
                  np.log1p(max(row.get('Issue_Size(crores)',100),1))+1)/m['aprx_max'],
        'mwr5':float(latest['mwr5']),'mwr10':float(latest['mwr10']),
        'mvol':float(latest['mvol']),'ipc':2,
        'gmp_acc':float(latest['gmp_acc']),
        'gpr':row.get('gmp_closing_day',0)/(row.get('Offer Price',100)+1),
        'al10':float(latest['al10']),'ag10':float(latest['ag10']),
        'grank':float((m['df']['gmp_closing_gain_pct'].tail(20)<
                       row.get('gmp_closing_gain_pct',0)).mean()),
    }])
    Xnew=make_features(feat_row)
    Xsc=m['sc'].transform(Xnew.values)

    # per-model, subscription-free win probabilities for the tier ladder
    win_probs={}
    if m.get('winm') and m.get('scw') is not None:
        Xw=m['scw'].transform(make_win_features(feat_row).values)
        for nm, mdl in m['winm'].items():
            win_probs[nm]=int(round(float(mdl.predict_proba(Xw)[0,1])*100))
    # verdict/confidence use the subscription-free ensemble (stable during bidding)
    win_prob=(sum(win_probs.values())/len(win_probs)/100.0) if win_probs \
             else float(m['rf'].predict_proba(Xsc)[0,1])
    win_score=int(win_prob*100)
    gmp=float(row.get('gmp_closing_gain_pct',0))
    apply=win_score>=59 and gmp>=7

    mid=float(m['gq'][0.50].predict(Xsc)[0])+m['bias']
    lo=float(m['gq'][0.03].predict(Xsc)[0])-m['buf_lo']
    hi=float(m['gq'][0.90].predict(Xsc)[0])+m['buf_hi']

    big_prob=float(m['xgb_big'].predict_proba(Xsc)[0,1])
    big_win=big_prob>=0.40

    rii=float(row.get('RII',1) or 1)
    allot_pct=min(100.0, round(100/max(rii,0.01),1))

    rag_vec=[float(row.get(f,0)) if f!='sector_code' else sec_code
             for f in m['RAG_F']]
    rag_vec=[0.0 if (v is None or np.isnan(v)) else v for v in rag_vec]
    ds,ix=m['knn'].kneighbors(m['sr'].transform([rag_vec]))
    df_ref=m['df']; yl=m['yl']
    neighbours=[{
        'name':str(df_ref.iloc[n]['IPO_Name']),
        'year':int(df_ref.iloc[n]['Date'].year),
        'sector':str(df_ref.iloc[n]['sector']),
        'gmp':float(df_ref.iloc[n]['gmp_closing_gain_pct']),
        'sub':float(df_ref.iloc[n]['Total']),
        'listing':float(yl[n]),
        'win':int(df_ref.iloc[n]['win']),
        'sim':round(float(1-ds[0][j+1]),2),
    } for j,n in enumerate(ix[0][1:6])]
    rag_avg=float(np.mean([n['listing'] for n in neighbours]))
    rag_wr=float(np.mean([n['win'] for n in neighbours]))

    pca_vec=[float(row.get(f,0)) if f!='sector_code' else sec_code
             for f in m['PCA_F']]
    pca_vec=[0.0 if (v is None or np.isnan(v)) else v for v in pca_vec]
    pca_sc=m['sp'].transform([pca_vec])
    pca_err=float(np.mean((m['pca_m'].inverse_transform(
        m['pca_m'].transform(pca_sc))-pca_sc)**2))
    pca_pct=float((m['all_pca']<=pca_err).mean())
    pca_flag=pca_err>m['pca_thr']

    confidence=int(round(100*(win_prob if apply else (1-win_prob))))
    confidence=max(5,min(99,confidence))

    conflicts=[]
    if float(row.get('ofs_pct',0) or 0)>=100:
        conflicts.append("100% OFS, promoters selling entire stake, company gets Rs 0")
    if float(row.get('QIB',0) or 0)<1 and gmp>10:
        conflicts.append(f"QIB only {row.get('QIB',0):.1f}x despite GMP {gmp:+.1f}%, institutions not convinced")
    if win_prob>0.7 and rag_wr<0.5:
        conflicts.append(f"ML bullish ({win_prob:.0%}) but similar past IPOs only {rag_wr:.0%} WR")
    sec_hot_cold=""
    if sec:
        if sec.get('recent_wr',0.7)<sec.get('all_wr',0.7)-0.1:
            sec_hot_cold=f"COLD (recent {sec['recent_wr']:.0%} vs {sec['all_wr']:.0%} historical)"
            conflicts.append(f"{sector} sector is cold, {sec_hot_cold}")
        elif sec.get('recent_wr',0.7)>sec.get('all_wr',0.7)+0.1:
            sec_hot_cold=f"HOT (recent {sec['recent_wr']:.0%} vs {sec['all_wr']:.0%} historical)"

    if not apply:
        label="[X] SKIP"
    elif win_score>=90:
        label=" STRONG APPLY"
    elif win_score>=80:
        label="[OK] STRONG APPLY"
    else:
        label="[OK] APPLY"

    return {
        'signal1_win_score':win_score,
        'signal1_win_probs':win_probs,
        'signal1_apply':apply,
        'signal1_label':label,
        'signal2_lo':round(lo,1),
        'signal2_mid':round(mid,1),
        'signal2_hi':round(hi,1),
        'signal3_big_prob':round(big_prob,3),
        'signal3_big_win':big_win,
        'signal4_allot_pct':allot_pct,
        'confidence':confidence,
        'pca_flagged':pca_flag,
        'rag_avg':round(rag_avg,1),
        'rag_wr':round(rag_wr,2),
        'neighbours':neighbours,
        'conflicts':conflicts,
        'sec_hot_cold':sec_hot_cold,
    }

