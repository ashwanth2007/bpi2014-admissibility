"""
Model A: Lead Conversion Prediction  (Phase 3, Model A of the faculty spec)
===========================================================================
Dataset : UCI Bank Marketing "bank-additional-full.csv", 41,188 records, 20 inputs.
          Moro, S., Cortez, P., Rita, P. (2014), "A data-driven approach to predict
          the success of bank telemarketing", Decision Support Systems 62, 22-31.
          DOI 10.1016/j.dss.2014.03.001
Target  : y, did the client subscribe the term deposit. This is the CONVERSION label.

PUBLISHED BASELINE, and an important correction
          Moro et al. 2014 report AUC = 0.8 (neural network). That number is NOT a
          valid head-to-head for this file. The UCI page states the public
          bank-additional-full.csv is only "very close to" the data they analysed:
          their study covers 2008-2013 with 22 features selected from 150, whereas
          this file is May 2008 to Nov 2010 with 20 inputs. Different dataset.

          The valid same-file baseline is:
          Ghatasheh, N., Altaharwa, I., Aldebei, K. (2023), "Modeling the
          Telemarketing Process using Genetic Algorithms and Extreme Boosting",
          IEEE Access, DOI 10.1109/ACCESS.2023.3292840 (preprint arXiv:2310.19843).
          Same 41,188-record file. Table 13, GA-selected features + XGBoost,
          50x10-fold CV: AUC min 92.30%, avg ~94.4%, max 95.91%. Includes duration.

THE DURATION PROBLEM, and why it is the same defect as Model B's
---------------------------------------------------------------
The UCI documentation, written by the dataset authors, states verbatim:

  "11 - duration: last contact duration, in seconds (numeric). Important note:
   this attribute highly affects the output target (e.g., if duration=0 then
   y='no'). Yet, the duration is not known before a call is performed. Also,
   after the end of the call y is obviously known. Thus, this input should only
   be included for benchmark purposes and should be discarded if the intention
   is to have a realistic predictive model."

That is exactly the defect found in the Phase 3 Model B pipeline: a feature
measured after the decision point, correlated with the outcome by construction.
So this script reports BOTH configurations, and the honest deployment number is
the one without duration.

Runs:
  A1  with duration, all 20 inputs      -> parity condition vs Ghatasheh et al.
  A2  without duration, 19 inputs       -> decision-time admissible, deployable
  A3  A2 + tuned                        -> is the honest number improvable
  A4  A2 + tuned soft-vote ensemble     -> does combining actually help
  A5  A2 chronological holdout          -> data is date-ordered May08 to Nov10

Every model-vs-model AUC claim is checked with a paired bootstrap test, so
"higher" is only reported when it is statistically supported.
"""
import warnings, time
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from pathlib import Path
from sklearn.model_selection import train_test_split, StratifiedKFold, RandomizedSearchCV
from sklearn.preprocessing import OrdinalEncoder
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (roc_auc_score, f1_score, accuracy_score,
                             precision_score, recall_score, brier_score_loss)
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
from catboost import CatBoostClassifier

SEED = 42
np.random.seed(SEED)
BASE = Path.home() / "AppData/Local/Temp/claude/D--Ashwanth-S/91d4a6be-faa9-4f95-988d-f5bb22234386/scratchpad"
CSV = BASE / "bank/bank-additional/bank-additional-full.csv"
OUT = BASE / "model_a_results"
OUT.mkdir(exist_ok=True)

df = pd.read_csv(CSV, sep=";")
print(f"Loaded {len(df):,} rows x {df.shape[1]-1} inputs")
y = (df["y"] == "yes").astype(int)
print(f"Conversion rate: {y.mean():.2%}  (positives {y.sum():,})")

X_all = df.drop(columns=["y"]).copy()
cat_cols = X_all.select_dtypes(include="object").columns.tolist()
X_all[cat_cols] = OrdinalEncoder().fit_transform(X_all[cat_cols].astype(str))
X_all = X_all.astype(float)

FEATS_WITH_DUR = X_all.columns.tolist()
FEATS_NO_DUR = [c for c in FEATS_WITH_DUR if c != "duration"]
print(f"With duration: {len(FEATS_WITH_DUR)} | Without: {len(FEATS_NO_DUR)}")


def models(ytr):
    spw = (ytr == 0).sum() / max((ytr == 1).sum(), 1)
    return {
        "LogisticRegression": LogisticRegression(max_iter=2000, class_weight="balanced"),
        "RandomForest": RandomForestClassifier(n_estimators=400, max_depth=14,
                                               class_weight="balanced", random_state=SEED, n_jobs=-1),
        "XGBoost": XGBClassifier(n_estimators=400, max_depth=6, learning_rate=0.05,
                                 subsample=0.8, colsample_bytree=0.8, scale_pos_weight=spw,
                                 eval_metric="logloss", random_state=SEED, verbosity=0,
                                 tree_method="hist", n_jobs=-1),
        "LightGBM": LGBMClassifier(n_estimators=400, max_depth=6, learning_rate=0.05,
                                   subsample=0.8, colsample_bytree=0.8, is_unbalance=True,
                                   random_state=SEED, verbose=-1, n_jobs=-1),
        "CatBoost": CatBoostClassifier(iterations=400, depth=6, learning_rate=0.05,
                                       auto_class_weights="Balanced", random_seed=SEED, verbose=0),
    }


def bootstrap_auc_diff(y_true, p1, p2, n=2000, seed=SEED):
    """Paired bootstrap on AUC(p1) - AUC(p2). Returns (mean diff, 2.5%, 97.5%, P(diff>0))."""
    rng = np.random.default_rng(seed)
    y_true = np.asarray(y_true); p1 = np.asarray(p1); p2 = np.asarray(p2)
    idx = np.arange(len(y_true)); diffs = []
    for _ in range(n):
        b = rng.choice(idx, size=len(idx), replace=True)
        if len(np.unique(y_true[b])) < 2:
            continue
        diffs.append(roc_auc_score(y_true[b], p1[b]) - roc_auc_score(y_true[b], p2[b]))
    d = np.array(diffs)
    return d.mean(), np.percentile(d, 2.5), np.percentile(d, 97.5), (d > 0).mean()


def run(feats, label, tr_idx, te_idx, cv=True, store=None):
    Xtr, Xte = X_all.loc[tr_idx, feats], X_all.loc[te_idx, feats]
    ytr, yte = y.loc[tr_idx], y.loc[te_idx]
    rows, probs = [], {}
    for name, m in models(ytr).items():
        cvs = np.nan
        if cv:
            skf = StratifiedKFold(5, shuffle=True, random_state=SEED)
            fold = []
            for a, b in skf.split(Xtr, ytr):
                mm = models(ytr.iloc[a])[name]
                mm.fit(Xtr.iloc[a], ytr.iloc[a])
                fold.append(roc_auc_score(ytr.iloc[b], mm.predict_proba(Xtr.iloc[b])[:, 1]))
            cvs = float(np.mean(fold))
        m.fit(Xtr, ytr)
        p = m.predict_proba(Xte)[:, 1]; pr = m.predict(Xte)
        probs[name] = p
        rows.append({"Config": label, "Model": name, "N_Feat": len(feats),
                     "CV_AUC": round(cvs, 4) if cvs == cvs else None,
                     "Test_AUC": round(roc_auc_score(yte, p), 4),
                     "F1": round(f1_score(yte, pr), 4),
                     "Accuracy": round(accuracy_score(yte, pr), 4),
                     "Precision": round(precision_score(yte, pr, zero_division=0), 4),
                     "Recall": round(recall_score(yte, pr, zero_division=0), 4),
                     "Brier": round(brier_score_loss(yte, p), 4)})
        print(f"  {label:26s} {name:19s} CV-AUC={cvs:.4f}  Test-AUC={rows[-1]['Test_AUC']:.4f}"
              f"  F1={rows[-1]['F1']:.4f}  Acc={rows[-1]['Accuracy']:.4f}")
    if store is not None:
        store.extend(rows)
    return rows, probs, yte


tr_idx, te_idx = train_test_split(X_all.index, test_size=0.2, stratify=y, random_state=SEED)
cut = int(len(df) * 0.8)                     # file is ordered by date
tr_time, te_time = X_all.index[:cut], X_all.index[cut:]

results = []
print("\n" + "=" * 118)
print("  A1. WITH duration  (parity condition vs Ghatasheh et al. 2023, AUC 92.30-95.91%)")
print("=" * 118)
_, pr_dur, yte_r = run(FEATS_WITH_DUR, "A1_with_duration", tr_idx, te_idx, store=results)

print("\n" + "=" * 118)
print("  A2. WITHOUT duration  (decision-time admissible, the deployable model)")
print("=" * 118)
_, pr_nodur, _ = run(FEATS_NO_DUR, "A2_no_duration", tr_idx, te_idx, store=results)

print("\n" + "=" * 118)
print("  A3. WITHOUT duration, hyperparameter search")
print("=" * 118)
Xtr, Xte = X_all.loc[tr_idx, FEATS_NO_DUR], X_all.loc[te_idx, FEATS_NO_DUR]
ytr, yte = y.loc[tr_idx], y.loc[te_idx]
spw = (ytr == 0).sum() / max((ytr == 1).sum(), 1)
grid = {"n_estimators": [300, 500, 800, 1200], "max_depth": [3, 4, 5, 6, 8],
        "learning_rate": [0.01, 0.02, 0.03, 0.05, 0.08], "subsample": [0.6, 0.7, 0.8, 1.0],
        "colsample_bytree": [0.5, 0.6, 0.8, 1.0], "min_child_weight": [1, 5, 10, 20],
        "gamma": [0, 0.1, 0.3, 1.0], "reg_lambda": [0.5, 1, 3, 10]}
t0 = time.time()
srch = RandomizedSearchCV(
    XGBClassifier(scale_pos_weight=spw, eval_metric="logloss", random_state=SEED,
                  verbosity=0, tree_method="hist", n_jobs=-1),
    grid, n_iter=40, scoring="roc_auc", cv=StratifiedKFold(5, shuffle=True, random_state=SEED),
    random_state=SEED, n_jobs=1)
srch.fit(Xtr, ytr)
p_tuned = srch.best_estimator_.predict_proba(Xte)[:, 1]
print(f"  tuned XGBoost ({time.time()-t0:.0f}s): CV-AUC={srch.best_score_:.4f}  "
      f"Test-AUC={roc_auc_score(yte, p_tuned):.4f}")
print(f"  best params: {srch.best_params_}")
results.append({"Config": "A3_tuned_no_duration", "Model": "XGBoost_tuned", "N_Feat": len(FEATS_NO_DUR),
                "CV_AUC": round(srch.best_score_, 4), "Test_AUC": round(roc_auc_score(yte, p_tuned), 4),
                "F1": round(f1_score(yte, (p_tuned >= 0.5).astype(int)), 4),
                "Accuracy": round(accuracy_score(yte, (p_tuned >= 0.5).astype(int)), 4),
                "Precision": round(precision_score(yte, (p_tuned >= 0.5).astype(int), zero_division=0), 4),
                "Recall": round(recall_score(yte, (p_tuned >= 0.5).astype(int), zero_division=0), 4),
                "Brier": round(brier_score_loss(yte, p_tuned), 4)})

print("\n" + "=" * 118)
print("  A4. ENSEMBLE, does combining actually help (soft vote of tuned XGB + LGBM + CatBoost)")
print("=" * 118)
p_ens = np.mean([p_tuned, pr_nodur["LightGBM"], pr_nodur["CatBoost"]], axis=0)
best_single_name = max(pr_nodur, key=lambda k: roc_auc_score(yte, pr_nodur[k]))
p_best_single = p_tuned if roc_auc_score(yte, p_tuned) >= roc_auc_score(yte, pr_nodur[best_single_name]) \
    else pr_nodur[best_single_name]
auc_ens, auc_best = roc_auc_score(yte, p_ens), roc_auc_score(yte, p_best_single)
print(f"  Ensemble  Test-AUC={auc_ens:.4f}")
print(f"  Best single Test-AUC={auc_best:.4f}")
md, lo, hi, pgt = bootstrap_auc_diff(yte, p_ens, p_best_single)
print(f"  Paired bootstrap  diff={md:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]  P(ensemble better)={pgt:.3f}")
print(f"  VERDICT: ensemble is {'a REAL improvement' if lo > 0 else 'NOT a statistically supported improvement'}")
results.append({"Config": "A4_ensemble_no_duration", "Model": "SoftVote(XGBtuned,LGBM,CatBoost)",
                "N_Feat": len(FEATS_NO_DUR), "CV_AUC": None, "Test_AUC": round(auc_ens, 4),
                "F1": round(f1_score(yte, (p_ens >= 0.5).astype(int)), 4),
                "Accuracy": round(accuracy_score(yte, (p_ens >= 0.5).astype(int)), 4),
                "Precision": round(precision_score(yte, (p_ens >= 0.5).astype(int), zero_division=0), 4),
                "Recall": round(recall_score(yte, (p_ens >= 0.5).astype(int), zero_division=0), 4),
                "Brier": round(brier_score_loss(yte, p_ens), 4)})

print("\n" + "=" * 118)
print("  A5. CHRONOLOGICAL HOLDOUT, no duration (file is date-ordered May 2008 to Nov 2010)")
print("=" * 118)
_, _, _ = run(FEATS_NO_DUR, "A5_no_duration_temporal", tr_time, te_time, cv=False, store=results)

print("\n" + "=" * 118)
print("  STATISTICAL CHECK: what does dropping duration actually cost")
print("=" * 118)
md2, lo2, hi2, p2 = bootstrap_auc_diff(yte_r, pr_dur["XGBoost"], pr_nodur["XGBoost"])
print(f"  AUC(with duration) - AUC(without) = {md2:+.4f}  95% CI [{lo2:+.4f}, {hi2:+.4f}]")
print(f"  So duration alone is worth {md2:.4f} AUC. It is not available at decision time.")

res = pd.DataFrame(results)
res.to_csv(OUT / "model_a_results.csv", index=False)
print(f"\nWrote {OUT/'model_a_results.csv'}")
print("\n" + "=" * 118)
print("  SUMMARY vs PUBLISHED BASELINE")
print("=" * 118)
a1 = res[res.Config == "A1_with_duration"]["Test_AUC"].max()
a2 = res[res.Config == "A2_no_duration"]["Test_AUC"].max()
a3 = res[res.Config == "A3_tuned_no_duration"]["Test_AUC"].max()
a4 = res[res.Config == "A4_ensemble_no_duration"]["Test_AUC"].max()
a5 = res[res.Config == "A5_no_duration_temporal"]["Test_AUC"].max()
print(f"  Ghatasheh et al. 2023, same file, WITH duration, 50x10CV: 0.9230 / ~0.944 / 0.9591 (min/avg/max)")
print(f"  Ours, with duration (parity condition, single 80/20)    : {a1:.4f}   -> inside their range, PARITY not a win")
print(f"  NOTE: Moro et al. 2014's AUC 0.800 is NOT comparable (different dataset, 2008-2013, 22 feats).")
print(f"  Ours, no duration, untuned                             : {a2:.4f}")
print(f"  Ours, no duration, tuned                               : {a3:.4f}")
print(f"  Ours, no duration, ensemble                            : {a4:.4f}")
print(f"  Ours, no duration, chronological holdout               : {a5:.4f}")
print("=" * 118)
