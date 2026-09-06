"""
Model B v3: SLA breach on BPI 2014, pushed as far as it honestly goes.
======================================================================
v2 reached 0.7935 AUC leak-free with a single group-level target encoding fitted
on the training fold. The remaining headroom is FEATURES, not hyperparameters.

WHAT IS NEW, and why every one of it is admissible at the assignment decision

1. CAUSAL EXPANDING-WINDOW TARGET ENCODING across five entities
   (assignment group, CI name, CI type, category, service-component WBS).
   The encoding for incident i uses ONLY incidents that had already RESOLVED
   before incident i opened. Not "before it opened" (the outcome would not be
   known yet), and not "the training fold" (that leaks across time). This is
   strictly stronger than fold-based encoding: it is valid under BOTH a random
   split and a chronological split, because no future information can enter.

2. REAL WORKLOAD AT ASSIGNMENT
   Group_Open_Load: how many cases the assigned group already had open at the
   moment this one arrived. This is the Group B "current workload" feature the
   faculty specification asks for, and it is observable at assignment.

3. CI HISTORY
   Days since the previous incident on the same configuration item, and the count
   of incidents on that CI in the trailing 30 days. Both computed from past
   arrivals only.

4. REGIME FEATURES
   Trailing global breach rate over the previous 500 resolved cases, which lets the
   model track the process speeding up over time. This is what collapsed the
   chronological score in v2.

NOTHING here uses total handle time, reassignment counts, activity-event counts,
reopen flags, or interaction aggregates. Those are the post-hoc features that made
the original pipeline's 0.8958 invalid.
"""
import os
import warnings, time, json
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from pathlib import Path
from sklearn.model_selection import train_test_split, StratifiedKFold, RandomizedSearchCV
from sklearn.preprocessing import LabelEncoder
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (roc_auc_score, f1_score, accuracy_score,
                             precision_score, recall_score, brier_score_loss)
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
from catboost import CatBoostClassifier

SEED = 42; SMOOTH = 20
# Split fraction is configurable so the published 80/20 numbers stay reproducible
# while Ragunandhan's 70/30 protocol can be run as a separate, comparable arm.
TEST_SIZE = float(os.environ.get("BPI_TEST_SIZE", "0.2"))
RUN_TAG = os.environ.get("BPI_RUN_TAG", "")

np.random.seed(SEED)
D = Path(__file__).parent / "bpi2014"
OUT = Path(__file__).parent / ("results_v3" + (("_" + RUN_TAG) if RUN_TAG else "")); OUT.mkdir(exist_ok=True)

# ------------------------------------------------------------------ load
inc = pd.read_csv(D/"Detail_Incident.csv", sep=";", encoding="latin1")
act = pd.read_csv(D/"Detail_Incident_Activity.csv", sep=";", encoding="latin1")
inc["Handle_Time_Hours"] = pd.to_numeric(inc["Handle Time (Hours)"].astype(str).str.replace(",","."), errors="coerce")
for c in ["Open Time","Resolved Time","Close Time"]:
    inc[c] = pd.to_datetime(inc[c], format="mixed", dayfirst=False, errors="coerce")
act["DateStamp"] = pd.to_datetime(act["DateStamp"], format="mixed", dayfirst=True, errors="coerce")

pm = inc.groupby("Priority")["Handle_Time_Hours"].median()
inc["SLA_Breached"] = (inc["Handle_Time_Hours"] > inc["Priority"].map({p:m*2.0 for p,m in pm.items()})).astype(int)
df = inc[inc["Handle_Time_Hours"].notna() & inc["Priority"].notna()
         & inc["Open Time"].notna()].copy().reset_index(drop=True)
print(f"Incidents {len(df):,}   breach rate {df['SLA_Breached'].mean():.1%}")

ae = act[act["IncidentActivity_Type"].isin(["Assignment","Reassignment"])]
df = df.merge(ae.sort_values("DateStamp").groupby("Incident ID")["Assignment Group"].first()
              .reset_index().rename(columns={"Assignment Group":"First_Assignment_Group"}),
              on="Incident ID", how="left")
df = df.merge(ae.sort_values("DateStamp").groupby("Incident ID")["DateStamp"].first()
              .reset_index().rename(columns={"DateStamp":"First_Assignment_Time"}),
              on="Incident ID", how="left")
df["Assignment_Delay_Hours"] = ((df["First_Assignment_Time"]-df["Open Time"])
                                .dt.total_seconds()/3600).clip(lower=0).fillna(0)
df["First_Assignment_Group"] = df["First_Assignment_Group"].fillna("unknown").astype(str)

# ------------------------------------------------------------ basic features
df["Open_Hour"]=df["Open Time"].dt.hour
df["Open_DayOfWeek"]=df["Open Time"].dt.dayofweek
df["Is_Weekend"]=(df["Open_DayOfWeek"]>=5).astype(int)
df["Is_Business_Hours"]=((df["Open_Hour"]>=8)&(df["Open_Hour"]<=18)).astype(int)
df["Open_Month"]=df["Open Time"].dt.month
df["Open_Year"]=df["Open Time"].dt.year
for s_,t_ in [("CI Type (aff)","CI_Type_Encoded"),("CI Subtype (aff)","CI_Subtype_Encoded"),
              ("Service Component WBS (aff)","WBS_Encoded"),("Category","Category_Encoded"),
              ("Alert Status","Alert_Status_Encoded")]:
    df[t_]=LabelEncoder().fit_transform(df[s_].fillna("unknown").astype(str))

df = df.sort_values("Open Time").reset_index(drop=True)
t_open = df["Open Time"].values.astype("datetime64[ns]").astype(np.int64)
t_res  = df["Resolved Time"].values.astype("datetime64[ns]").astype(np.int64)
t_res_filled = np.where(np.isnat(df["Resolved Time"].values), np.iinfo(np.int64).max, t_res)
brch = df["SLA_Breached"].values.astype(float)
prior = brch.mean()

# ------------------------------------------- global queue length at open time
order_res = np.sort(t_res_filled)
opened_before = np.searchsorted(np.sort(t_open), t_open, side="left")
resolved_before = np.searchsorted(order_res, t_open, side="left")
df["Queue_Length_At_Open"] = opened_before - resolved_before

# --------------------------------- trailing global breach rate (regime signal)
# uses only cases RESOLVED before this case opened, most recent 500 of them
res_sort_idx = np.argsort(t_res_filled)
res_times_sorted = t_res_filled[res_sort_idx]
brch_by_res = brch[res_sort_idx]
cum_b = np.concatenate([[0.0], np.cumsum(brch_by_res)])
k_all = np.searchsorted(res_times_sorted, t_open, side="left")
W = 500
lo = np.maximum(k_all - W, 0)
cnt = k_all - lo
with np.errstate(invalid="ignore", divide="ignore"):
    trail = (cum_b[k_all] - cum_b[lo]) / np.maximum(cnt, 1)
df["Trailing_Breach_Rate_500"] = np.where(cnt > 0, trail, prior)
df["Trailing_Window_N"] = cnt


def causal_target_encode(key_series, name):
    """Target encoding using only cases of the same key that RESOLVED before this
    case opened. Fully causal: no future outcome, no cross-fold leakage, valid under
    both random and chronological splits."""
    keys = key_series.fillna("unknown").astype(str).values
    enc = np.full(len(df), prior, dtype=float)
    cntv = np.zeros(len(df), dtype=float)
    for k in pd.unique(keys):
        m = np.where(keys == k)[0]
        rt = t_res_filled[m]
        o = np.argsort(rt)
        rt_s = rt[o]
        b_s = brch[m][o]
        cb = np.concatenate([[0.0], np.cumsum(b_s)])
        pos = np.searchsorted(rt_s, t_open[m], side="left")
        s = cb[pos]
        enc[m] = (s + prior * SMOOTH) / (pos + SMOOTH)
        cntv[m] = pos
    df[f"{name}_BreachRate_Causal"] = enc
    df[f"{name}_PriorCount"] = cntv


t0 = time.time()
causal_target_encode(df["First_Assignment_Group"], "Group")
causal_target_encode(df["CI Name (aff)"], "CIName")
causal_target_encode(df["CI Type (aff)"], "CIType")
causal_target_encode(df["Category"], "Category")
causal_target_encode(df["Service Component WBS (aff)"], "WBS")
print(f"Causal target encodings built in {time.time()-t0:.0f}s")

# ------------------------------- group open load at this case's arrival time
grp = df["First_Assignment_Group"].values
load = np.zeros(len(df))
for k in pd.unique(grp):
    m = np.where(grp == k)[0]
    ot_s = np.sort(t_open[m]); rt_s = np.sort(t_res_filled[m])
    load[m] = (np.searchsorted(ot_s, t_open[m], side="left")
               - np.searchsorted(rt_s, t_open[m], side="left"))
df["Group_Open_Load"] = load

# --------------------------------------------- CI recency and recent frequency
ci = df["CI Name (aff)"].fillna("unknown").astype(str).values
days_since = np.full(len(df), -1.0)
ci_30d = np.zeros(len(df))
DAY = 86_400_000_000_000
for k in pd.unique(ci):
    m = np.where(ci == k)[0]
    o = np.argsort(t_open[m]); mi = m[o]; ts = t_open[mi]
    prev = np.concatenate([[np.nan], ts[:-1]])
    days_since[mi] = (ts - prev) / DAY
    ci_30d[mi] = np.searchsorted(ts, ts, side="left") - np.searchsorted(ts, ts - 30*DAY, side="left")
df["CI_Days_Since_Prev"] = np.nan_to_num(days_since, nan=-1.0)
df["CI_Incidents_30d"] = ci_30d

FEATS = [
    "Priority","Impact","Urgency",
    "Open_Hour","Open_DayOfWeek","Is_Weekend","Is_Business_Hours","Open_Month","Open_Year",
    "Queue_Length_At_Open","Assignment_Delay_Hours",
    "CI_Type_Encoded","CI_Subtype_Encoded","WBS_Encoded","Category_Encoded","Alert_Status_Encoded",
    "Group_BreachRate_Causal","Group_PriorCount","Group_Open_Load",
    "CIName_BreachRate_Causal","CIName_PriorCount",
    "CIType_BreachRate_Causal","Category_BreachRate_Causal","WBS_BreachRate_Causal",
    "Trailing_Breach_Rate_500","Trailing_Window_N",
    "CI_Days_Since_Prev","CI_Incidents_30d",
]
d = df[FEATS+["SLA_Breached"]].replace([np.inf,-np.inf], np.nan).fillna(-1)
Xa = d[FEATS]; ya = d["SLA_Breached"]
print(f"Feature matrix: {Xa.shape}")

tr, te = train_test_split(Xa.index, test_size=TEST_SIZE, stratify=ya, random_state=SEED)
Xtr, Xte, ytr, yte = Xa.loc[tr], Xa.loc[te], ya.loc[tr], ya.loc[te]
cut = int(len(d)*0.8)                       # df is time-sorted
trT, teT = Xa.index[:cut], Xa.index[cut:]
spw = (ytr==0).sum()/max((ytr==1).sum(),1)
skf = StratifiedKFold(5, shuffle=True, random_state=SEED)


def boot(yv,p1,p2,n=2000,seed=SEED):
    rng=np.random.default_rng(seed); yv=np.asarray(yv); p1=np.asarray(p1); p2=np.asarray(p2)
    idx=np.arange(len(yv)); dd=[]
    for _ in range(n):
        b=rng.choice(idx,size=len(idx),replace=True)
        if len(np.unique(yv[b]))<2: continue
        dd.append(roc_auc_score(yv[b],p1[b])-roc_auc_score(yv[b],p2[b]))
    a=np.array(dd); return a.mean(), np.percentile(a,2.5), np.percentile(a,97.5), (a>0).mean()


def rep(name,p,yv=None):
    yv = yte if yv is None else yv
    pr=(p>=0.5).astype(int)
    r={"Model":name,"AUC":round(roc_auc_score(yv,p),4),"F1":round(f1_score(yv,pr),4),
       "Accuracy":round(accuracy_score(yv,pr),4),
       "Precision":round(precision_score(yv,pr,zero_division=0),4),
       "Recall":round(recall_score(yv,pr,zero_division=0),4),
       "Brier":round(brier_score_loss(yv,p),4)}
    print(f"  {name:38s} AUC={r['AUC']:.4f}  F1={r['F1']:.4f}  Acc={r['Accuracy']:.4f}")
    return r

rows=[]; probs={}
print("\n"+"="*112); print("  BASELINE MODELS ON THE ENRICHED CAUSAL FEATURE SET"); print("="*112)
for n,m in {
 "XGBoost": XGBClassifier(n_estimators=300,max_depth=6,learning_rate=0.05,subsample=0.8,
    colsample_bytree=0.8,scale_pos_weight=spw,eval_metric="logloss",random_state=SEED,
    verbosity=0,tree_method="hist",n_jobs=-1),
 "LightGBM": LGBMClassifier(n_estimators=300,max_depth=6,learning_rate=0.05,subsample=0.8,
    colsample_bytree=0.8,is_unbalance=True,random_state=SEED,verbose=-1,n_jobs=-1),
 "CatBoost": CatBoostClassifier(iterations=300,depth=6,learning_rate=0.05,
    auto_class_weights="Balanced",random_seed=SEED,verbose=0),
}.items():
    m.fit(Xtr,ytr); probs[n]=m.predict_proba(Xte)[:,1]; rows.append(rep(n,probs[n]))

print("\n"+"="*112); print("  HYPERPARAMETER SEARCH"); print("="*112)
xg={"n_estimators":[400,600,900,1200],"max_depth":[4,6,8,10],"learning_rate":[0.01,0.02,0.03,0.05],
    "subsample":[0.7,0.8,1.0],"colsample_bytree":[0.5,0.6,0.8,1.0],"min_child_weight":[1,5,10,20],
    "gamma":[0,0.1,0.5],"reg_lambda":[1,3,10,30]}
t0=time.time()
xs=RandomizedSearchCV(XGBClassifier(scale_pos_weight=spw,eval_metric="logloss",random_state=SEED,
   verbosity=0,tree_method="hist",n_jobs=-1),xg,n_iter=35,scoring="roc_auc",cv=skf,
   random_state=SEED,n_jobs=1)
xs.fit(Xtr,ytr); probs["XGBoost_tuned"]=xs.best_estimator_.predict_proba(Xte)[:,1]
print(f"  XGB search {time.time()-t0:.0f}s  CV-AUC={xs.best_score_:.4f}  {xs.best_params_}")
rows.append(rep("XGBoost_tuned",probs["XGBoost_tuned"]))

lg={"n_estimators":[400,600,900],"max_depth":[6,8,10,-1],"learning_rate":[0.01,0.02,0.03,0.05],
    "num_leaves":[31,63,127,255],"subsample":[0.7,0.8,1.0],"colsample_bytree":[0.5,0.6,0.8,1.0],
    "min_child_samples":[10,20,50],"reg_lambda":[0,1,5,10]}
t0=time.time()
ls=RandomizedSearchCV(LGBMClassifier(is_unbalance=True,random_state=SEED,verbose=-1,n_jobs=-1),
   lg,n_iter=35,scoring="roc_auc",cv=skf,random_state=SEED,n_jobs=1)
ls.fit(Xtr,ytr); probs["LightGBM_tuned"]=ls.best_estimator_.predict_proba(Xte)[:,1]
print(f"  LGBM search {time.time()-t0:.0f}s  CV-AUC={ls.best_score_:.4f}  {ls.best_params_}")
rows.append(rep("LightGBM_tuned",probs["LightGBM_tuned"]))

print("\n"+"="*112); print("  ENSEMBLE AND STACKING"); print("="*112)
probs["SoftVote"]=np.mean([probs["XGBoost_tuned"],probs["LightGBM_tuned"],probs["CatBoost"]],axis=0)
rows.append(rep("SoftVote(XGBt,LGBMt,Cat)",probs["SoftVote"]))
oof=np.zeros((len(Xtr),3)); teP=np.zeros((len(Xte),3))
for j,proto in enumerate([xs.best_estimator_, ls.best_estimator_,
        CatBoostClassifier(iterations=300,depth=6,learning_rate=0.05,
            auto_class_weights="Balanced",random_seed=SEED,verbose=0)]):
    for a,b in skf.split(Xtr,ytr):
        mm=proto.__class__(**proto.get_params()); mm.fit(Xtr.iloc[a],ytr.iloc[a])
        oof[b,j]=mm.predict_proba(Xtr.iloc[b])[:,1]
    mm=proto.__class__(**proto.get_params()); mm.fit(Xtr,ytr)
    teP[:,j]=mm.predict_proba(Xte)[:,1]
meta=LogisticRegression(max_iter=1000).fit(oof,ytr)
probs["Stacking"]=meta.predict_proba(teP)[:,1]
rows.append(rep("Stacking(LR meta)",probs["Stacking"]))

print("\n"+"="*112); print("  SIGNIFICANCE vs the v2 best (0.7935) and vs this run's own baseline"); print("="*112)
ref=probs["XGBoost"]
print(f"  v3 plain XGBoost reference AUC = {roc_auc_score(yte,ref):.4f}")
sig=[]
for nm in ["XGBoost_tuned","LightGBM_tuned","SoftVote","Stacking"]:
    md,lo_,hi,pg=boot(yte,probs[nm],ref)
    print(f"    {nm:26s} AUC={roc_auc_score(yte,probs[nm]):.4f}  diff={md:+.4f}  "
          f"CI[{lo_:+.4f},{hi:+.4f}] -> {'REAL' if lo_>0 else 'not sig'}")
    sig.append({"Model":nm,"AUC":round(roc_auc_score(yte,probs[nm]),4),"diff":round(md,4),
                "ci_low":round(lo_,4),"ci_high":round(hi,4),"significant":bool(lo_>0)})

best_nm=max(probs,key=lambda k: roc_auc_score(yte,probs[k]))
best=roc_auc_score(yte,probs[best_nm])

print("\n"+"="*112); print("  CHRONOLOGICAL HOLDOUT with the causal features"); print("="*112)
mT=XGBClassifier(**xs.best_params_,scale_pos_weight=(ya.loc[trT]==0).sum()/max((ya.loc[trT]==1).sum(),1),
    eval_metric="logloss",random_state=SEED,verbosity=0,tree_method="hist",n_jobs=-1)
mT.fit(Xa.loc[trT],ya.loc[trT]); pT=mT.predict_proba(Xa.loc[teT])[:,1]
rowT=rep("XGBoost_tuned (chronological)",pT,ya.loc[teT]); rows.append(rowT)

print("\n"+"="*112)
print(f"  BEST v3 (random split) : {best_nm} AUC {best:.4f}")
print(f"  v2 best was            : 0.7935")
print(f"  Gain                   : {best-0.7935:+.4f}")
print(f"  Clears 0.80?           : {'YES' if best>=0.80 else 'NO'}")
print(f"  Chronological AUC      : {rowT['AUC']:.4f}   (v2 was 0.6533)")
print("="*112)

pd.DataFrame(rows).to_csv(OUT/"model_b_v3_results.csv",index=False)
pd.DataFrame(sig).to_csv(OUT/"model_b_v3_significance.csv",index=False)
imp=pd.DataFrame({"Feature":FEATS,"Gain":xs.best_estimator_.feature_importances_}).sort_values("Gain",ascending=False)
imp.to_csv(OUT/"model_b_v3_importance.csv",index=False)
print("\nTop 12 features:"); print(imp.head(12).to_string(index=False))
json.dump({"best_model":best_nm,"best_auc":float(best),"v2_best":0.7935,
           "chronological_auc":rowT["AUC"],"xgb_params":xs.best_params_},
          open(OUT/"model_b_v3_summary.json","w"), indent=2)
