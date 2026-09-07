"""Model B (SLA breach, BPI 2014): tuning + ensemble, on the LEAK-FREE feature set.
Question 1: does hyperparameter tuning push the honest 0.7877 over 0.80?
Question 2: does combining models genuinely help, or does it just look like it?

Every "better" claim is tested with a paired bootstrap on the same test set, so a
gain is only reported as real when the 95% CI on the difference excludes zero.
"""
import warnings, time
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from pathlib import Path
from sklearn.model_selection import train_test_split, StratifiedKFold, RandomizedSearchCV
from sklearn.preprocessing import LabelEncoder
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (roc_auc_score, f1_score, accuracy_score,
                             precision_score, recall_score, brier_score_loss)
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
from catboost import CatBoostClassifier

import os
SEED = 42; SMOOTH = 20
np.random.seed(SEED)
# Split fraction and run tag are configurable so this arm can be run under the same
# 70/30 protocol as the rest of the paper rather than only at the original 80/20.
TEST_SIZE = float(os.environ.get("BPI_TEST_SIZE", "0.2"))
RUN_TAG = os.environ.get("BPI_RUN_TAG", "")
D = Path(__file__).parent / "bpi2014"
OUT = Path(__file__).parent / ("results_bpi_leakfree" + (("_" + RUN_TAG) if RUN_TAG else ""))
OUT.mkdir(exist_ok=True)

inc = pd.read_csv(D/"Detail_Incident.csv", sep=";", encoding="latin1")
act = pd.read_csv(D/"Detail_Incident_Activity.csv", sep=";", encoding="latin1")
inc["Handle_Time_Hours"] = pd.to_numeric(inc["Handle Time (Hours)"].astype(str).str.replace(",","."), errors="coerce")
for c in ["Open Time","Resolved Time","Close Time"]:
    inc[c] = pd.to_datetime(inc[c], format="mixed", dayfirst=False, errors="coerce")
act["DateStamp"] = pd.to_datetime(act["DateStamp"], format="mixed", dayfirst=True, errors="coerce")

pm = inc.groupby("Priority")["Handle_Time_Hours"].median()
inc["SLA_Breached"] = (inc["Handle_Time_Hours"] > inc["Priority"].map({p:m*2.0 for p,m in pm.items()})).astype(int)
df = inc[inc["Handle_Time_Hours"].notna() & inc["Priority"].notna()].copy()

ae = act[act["IncidentActivity_Type"].isin(["Assignment","Reassignment"])]
df = df.merge(ae.sort_values("DateStamp").groupby("Incident ID")["Assignment Group"].first()
              .reset_index().rename(columns={"Assignment Group":"First_Assignment_Group"}),
              on="Incident ID", how="left")
df = df.merge(ae.sort_values("DateStamp").groupby("Incident ID")["DateStamp"].first()
              .reset_index().rename(columns={"DateStamp":"First_Assignment_Time"}),
              on="Incident ID", how="left")
df["Assignment_Delay_Hours"] = ((df["First_Assignment_Time"]-df["Open Time"]).dt.total_seconds()/3600).clip(lower=0).fillna(0)
df["Open_Hour"]=df["Open Time"].dt.hour; df["Open_DayOfWeek"]=df["Open Time"].dt.dayofweek
df["Is_Weekend"]=(df["Open_DayOfWeek"]>=5).astype(int)
df["Is_Business_Hours"]=((df["Open_Hour"]>=8)&(df["Open_Hour"]<=18)).astype(int)
df["Open_Month"]=df["Open Time"].dt.month
ds=df.sort_values("Open Time"); ql=[]; os_=[]
for o,r in zip(ds["Open Time"].values, ds["Resolved Time"].values):
    os_=[x for x in os_ if x>o or pd.isna(x)]; ql.append(len(os_))
    if pd.notna(r): os_.append(r)
df = df.merge(ds.assign(Queue_Length_At_Open=ql)[["Incident ID","Queue_Length_At_Open"]], on="Incident ID", how="left")
for s,t in [("CI Type (aff)","CI_Type_Encoded"),("CI Subtype (aff)","CI_Subtype_Encoded"),
            ("Service Component WBS (aff)","WBS_Encoded"),("Category","Category_Encoded"),
            ("Alert Status","Alert_Status_Encoded")]:
    df[t]=LabelEncoder().fit_transform(df[s].fillna("unknown").astype(str))

BASE=["Priority","Impact","Urgency","Open_Hour","Open_DayOfWeek","Is_Weekend","Is_Business_Hours",
      "Open_Month","Queue_Length_At_Open","Assignment_Delay_Hours","CI_Type_Encoded",
      "CI_Subtype_Encoded","WBS_Encoded","Category_Encoded","Alert_Status_Encoded"]
d = df[BASE+["SLA_Breached","First_Assignment_Group"]].dropna(subset=BASE+["SLA_Breached"]).copy()
d["First_Assignment_Group"]=d["First_Assignment_Group"].fillna("unknown").astype(str)
y=d["SLA_Breached"]; g=d["First_Assignment_Group"]
tr,te = train_test_split(d.index, test_size=TEST_SIZE, stratify=y, random_state=SEED)

def enc_fit(gg,yy):
    st=pd.DataFrame({"g":gg.values,"y":yy.values}).groupby("g")["y"].agg(["mean","count"])
    pr=float(yy.mean()); return (st["mean"]*st["count"]+pr*SMOOTH)/(st["count"]+SMOOTH), st["count"], pr
def enc_apply(X,gg,e):
    b,v,pr=e; X=X.copy()
    X["Group_Breach_Rate_TE"]=gg.map(b).fillna(pr).values
    X["Group_Volume_TE"]=gg.map(v).fillna(0).values
    return X

e=enc_fit(g.loc[tr],y.loc[tr])
Xtr=enc_apply(d.loc[tr,BASE],g.loc[tr],e); ytr=y.loc[tr]
Xte=enc_apply(d.loc[te,BASE],g.loc[te],e); yte=y.loc[te]
spw=(ytr==0).sum()/max((ytr==1).sum(),1)
skf=StratifiedKFold(5,shuffle=True,random_state=SEED)
print(f"Train {len(Xtr):,} | Test {len(Xte):,} | feats {Xtr.shape[1]} | breach {y.mean():.1%}\n")

def boot(y_true,p1,p2,n=2000,seed=SEED):
    rng=np.random.default_rng(seed); y_true=np.asarray(y_true); p1=np.asarray(p1); p2=np.asarray(p2)
    idx=np.arange(len(y_true)); ds_=[]
    for _ in range(n):
        b=rng.choice(idx,size=len(idx),replace=True)
        if len(np.unique(y_true[b]))<2: continue
        ds_.append(roc_auc_score(y_true[b],p1[b])-roc_auc_score(y_true[b],p2[b]))
    a=np.array(ds_); return a.mean(), np.percentile(a,2.5), np.percentile(a,97.5), (a>0).mean()

def metrics(name,p,cv=None):
    pr=(p>=0.5).astype(int)
    r={"Model":name,"CV_AUC":round(cv,4) if cv else None,"Test_AUC":round(roc_auc_score(yte,p),4),
       "F1":round(f1_score(yte,pr),4),"Accuracy":round(accuracy_score(yte,pr),4),
       "Precision":round(precision_score(yte,pr,zero_division=0),4),
       "Recall":round(recall_score(yte,pr,zero_division=0),4),
       "Brier":round(brier_score_loss(yte,p),4)}
    print(f"  {name:34s} CV-AUC={r['CV_AUC'] if r['CV_AUC'] else float('nan'):.4f}  "
          f"Test-AUC={r['Test_AUC']:.4f}  F1={r['F1']:.4f}  Acc={r['Accuracy']:.4f}")
    return r

rows=[]; probs={}

# ---- untuned baselines (the 0.7877 reference) ----
print("="*112); print("  UNTUNED BASELINES (leak-free)"); print("="*112)
base_models = {
 "XGBoost_untuned": XGBClassifier(n_estimators=300,max_depth=6,learning_rate=0.05,subsample=0.8,
     colsample_bytree=0.8,scale_pos_weight=spw,eval_metric="logloss",random_state=SEED,
     verbosity=0,tree_method="hist",n_jobs=-1),
 "LightGBM_untuned": LGBMClassifier(n_estimators=300,max_depth=6,learning_rate=0.05,subsample=0.8,
     colsample_bytree=0.8,is_unbalance=True,random_state=SEED,verbose=-1,n_jobs=-1),
 "CatBoost_untuned": CatBoostClassifier(iterations=300,depth=6,learning_rate=0.05,
     auto_class_weights="Balanced",random_seed=SEED,verbose=0),
 "RandomForest_untuned": RandomForestClassifier(n_estimators=300,max_depth=12,
     class_weight="balanced",random_state=SEED,n_jobs=-1),
}
for n,m in base_models.items():
    m.fit(Xtr,ytr); p=m.predict_proba(Xte)[:,1]; probs[n]=p; rows.append(metrics(n,p))

# ---- lean tuning ----
print("\n"+"="*112); print("  HYPERPARAMETER SEARCH (lean: 25 candidates x 5 folds, capped depth/estimators)"); print("="*112)
xg_grid={"n_estimators":[300,500,800],"max_depth":[4,5,6,8],"learning_rate":[0.02,0.03,0.05,0.08],
         "subsample":[0.7,0.8,1.0],"colsample_bytree":[0.6,0.8,1.0],"min_child_weight":[1,5,10,20],
         "gamma":[0,0.1,0.5],"reg_lambda":[1,3,10]}
t0=time.time()
xs=RandomizedSearchCV(XGBClassifier(scale_pos_weight=spw,eval_metric="logloss",random_state=SEED,
    verbosity=0,tree_method="hist",n_jobs=-1),xg_grid,n_iter=25,scoring="roc_auc",cv=skf,
    random_state=SEED,n_jobs=1)
xs.fit(Xtr,ytr); p_xgt=xs.best_estimator_.predict_proba(Xte)[:,1]; probs["XGBoost_tuned"]=p_xgt
print(f"  XGBoost search {time.time()-t0:.0f}s | best params: {xs.best_params_}")
rows.append(metrics("XGBoost_tuned",p_xgt,xs.best_score_))

lg_grid={"n_estimators":[300,500,800],"max_depth":[4,6,8,-1],"learning_rate":[0.02,0.03,0.05,0.08],
         "num_leaves":[15,31,63,127],"subsample":[0.7,0.8,1.0],"colsample_bytree":[0.6,0.8,1.0],
         "min_child_samples":[10,20,50,100],"reg_lambda":[0,1,5]}
t0=time.time()
lsr=RandomizedSearchCV(LGBMClassifier(is_unbalance=True,random_state=SEED,verbose=-1,n_jobs=-1),
    lg_grid,n_iter=25,scoring="roc_auc",cv=skf,random_state=SEED,n_jobs=1)
lsr.fit(Xtr,ytr); p_lgt=lsr.best_estimator_.predict_proba(Xte)[:,1]; probs["LightGBM_tuned"]=p_lgt
print(f"  LightGBM search {time.time()-t0:.0f}s | best params: {lsr.best_params_}")
rows.append(metrics("LightGBM_tuned",p_lgt,lsr.best_score_))

# ---- ensembles ----
print("\n"+"="*112); print("  ENSEMBLES"); print("="*112)
p_soft=np.mean([p_xgt,p_lgt,probs["CatBoost_untuned"]],axis=0); probs["SoftVote_3"]=p_soft
rows.append(metrics("SoftVote(XGBt,LGBMt,CatBoost)",p_soft))
p_soft4=np.mean([p_xgt,p_lgt,probs["CatBoost_untuned"],probs["RandomForest_untuned"]],axis=0)
probs["SoftVote_4"]=p_soft4
rows.append(metrics("SoftVote(4 models)",p_soft4))

# stacking with a logistic meta-learner on out-of-fold predictions
print("\n  Stacking (logistic meta-learner on 5-fold out-of-fold predictions)")
oof=np.zeros((len(Xtr),3)); teP=np.zeros((len(Xte),3))
learners=[("xgb",xs.best_estimator_),("lgb",lsr.best_estimator_),("cat",base_models["CatBoost_untuned"])]
for j,(nm,proto) in enumerate(learners):
    for a,b in skf.split(Xtr,ytr):
        m=proto.__class__(**proto.get_params()); m.fit(Xtr.iloc[a],ytr.iloc[a])
        oof[b,j]=m.predict_proba(Xtr.iloc[b])[:,1]
    m=proto.__class__(**proto.get_params()); m.fit(Xtr,ytr)
    teP[:,j]=m.predict_proba(Xte)[:,1]
meta=LogisticRegression(max_iter=1000).fit(oof,ytr)
p_stack=meta.predict_proba(teP)[:,1]; probs["Stacking"]=p_stack
rows.append(metrics("Stacking(LR meta)",p_stack))

# ---- significance: is any of it a REAL gain over the untuned XGBoost reference ----
print("\n"+"="*112); print("  PAIRED BOOTSTRAP vs untuned XGBoost (the 0.7877 reference)"); print("="*112)
ref=probs["XGBoost_untuned"]; ref_auc=roc_auc_score(yte,ref)
print(f"  Reference untuned XGBoost AUC = {ref_auc:.4f}\n")
sig=[]
for nm in ["XGBoost_tuned","LightGBM_tuned","SoftVote_3","SoftVote_4","Stacking"]:
    md,lo,hi,pg=boot(yte,probs[nm],ref)
    real = lo>0
    print(f"  {nm:26s} AUC={roc_auc_score(yte,probs[nm]):.4f}  diff={md:+.4f}  "
          f"95%CI[{lo:+.4f},{hi:+.4f}]  P(better)={pg:.3f}  -> {'REAL GAIN' if real else 'not significant'}")
    sig.append({"Model":nm,"Test_AUC":round(roc_auc_score(yte,probs[nm]),4),
                "Diff_vs_untuned":round(md,4),"CI_low":round(lo,4),"CI_high":round(hi,4),
                "P_better":round(pg,3),"Significant":bool(real)})

best_nm=max(probs,key=lambda k: roc_auc_score(yte,probs[k]))
best_auc=roc_auc_score(yte,probs[best_nm])
print("\n"+"="*112)
print(f"  BEST leak-free Model B: {best_nm} at AUC {best_auc:.4f}")
print(f"  Untuned reference     : {ref_auc:.4f}   (gain {best_auc-ref_auc:+.4f})")
print(f"  Clears 0.80 AUC?      : {'YES' if best_auc>=0.80 else 'NO'}")
print("="*112)

pd.DataFrame(rows).to_csv(OUT/"model_b_tuned_ensemble.csv",index=False)
pd.DataFrame(sig).to_csv(OUT/"model_b_significance.csv",index=False)
print(f"\nWrote {OUT/'model_b_tuned_ensemble.csv'} and model_b_significance.csv")
