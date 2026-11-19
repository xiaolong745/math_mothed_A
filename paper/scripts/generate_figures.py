from pathlib import Path
import csv, statistics, math
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'paper/figures'; OUT.mkdir(parents=True,exist_ok=True)
plt.rcParams.update({'font.size':10,'axes.titlesize':11,'axes.labelsize':10,'figure.dpi':160,'savefig.dpi':220,'font.family':'DejaVu Sans'})
# final matrix
rows=list(csv.DictReader((ROOT/'results/final_frozen_20260927/summary.csv').open()))
final=[r for r in rows if r.get('method')=='final_elite']
# grouped speedup
probs=['1','2','3']; cores=['2','3','4','5']; labels=['P1','P2','P3']
fig,ax=plt.subplots(figsize=(7.2,4.0))
import numpy as np
x=np.arange(len(cores)); width=.24
for j,p in enumerate(probs):
 vals=[]
 for k in cores:
  rr=[r for r in final if r['problem']==p and r['cores']==k]
  vals.append(sum(float(r['official_singlecore_speedup']) for r in rr)/len(rr))
 ax.bar(x+(j-1)*width,vals,width,label=labels[j])
for i,k in enumerate(cores):
 rr=[r for r in final if r['cores']==k];
 ax.text(i,0.05,f'{k}',ha='center',va='bottom',transform=ax.get_xaxis_transform(),fontsize=8)
ax.set_xticks(x); ax.set_xticklabels([f'{k} cores' for k in cores]); ax.set_ylabel('Mean official single-core speedup'); ax.set_ylim(0,5.2); ax.grid(axis='y',alpha=.25); ax.legend(frameon=False,ncol=3); fig.tight_layout(); fig.savefig(OUT/'speedup_by_problem_core.png'); plt.close(fig)
# improvement histogram / counts
imp=[float(r['relative_stage1_improvement'])*100 for r in final]
fig,ax=plt.subplots(figsize=(6.8,3.8)); ax.hist(imp,bins=16,color='#4472C4',edgecolor='white'); ax.axvline(0,color='black',lw=.8); ax.set_xlabel('Improvement over Stage 1 (%)'); ax.set_ylabel('Number of combinations'); ax.grid(axis='y',alpha=.25); fig.tight_layout(); fig.savefig(OUT/'stage1_improvement_hist.png'); plt.close(fig)
# evaluator calls grouped
rows_a=list(csv.DictReader((ROOT/'results/final_audit_20260927/group_metrics.csv').open()))
fig,ax=plt.subplots(figsize=(7.0,3.8));
for j,p in enumerate(probs):
 rr=[r for r in rows_a if r['problem']==p]; ax.plot([r['cores'] for r in rr],[float(r['uncached_evaluator_calls']) for r in rr],marker='o',label=labels[j])
ax.set_xlabel('Core count'); ax.set_ylabel('Uncached evaluator calls'); ax.set_xticks([2,3,4,5]); ax.grid(alpha=.25); ax.legend(frameon=False,ncol=3); fig.tight_layout(); fig.savefig(OUT/'evaluator_calls.png'); plt.close(fig)
# surrogate offline selector
s=list(csv.DictReader((ROOT/'results/surrogate_selector_ablation_20260927_v5/summary.csv').open()))
methods={'full':'Full pool','analytic':'Analytic proxy','surrogate':'Surrogate','random':'Random'}
fig,ax=plt.subplots(figsize=(6.8,3.8)); vals=[100*float(next(r for r in s if r['method']==m)['pool_optimum_rate']) for m in ['full','analytic','surrogate','random']]; ax.bar(list(methods.values()),vals,color=['#7F7F7F','#70AD47','#ED7D31','#A5A5A5']); ax.set_ylabel('Pool optimum retained (%)'); ax.set_ylim(0,110); ax.grid(axis='y',alpha=.25); ax.tick_params(axis='x',rotation=18); fig.tight_layout(); fig.savefig(OUT/'selector_comparison.png'); plt.close(fig)
# schematic DAG/pipeline using matplotlib
fig,ax=plt.subplots(figsize=(8.2,2.6)); ax.axis('off')
boxes=[('Input DAG','graph / tensor / edge'),('Graph model','Op-DAG + M/V'),('Portfolio','Simple\nRegionGrow\nBandComp'),('P refinement','FM + chain'),('Pi refinement','same-core reorder'),('Official evaluator','legal + makespan')]
xs=[.08,.25,.43,.59,.74,.91]
for x,(title,sub) in zip(xs,boxes):
 ax.text(x,.53,title,ha='center',va='center',bbox=dict(boxstyle='round,pad=.45',fc='#EAF2F8',ec='#4472C4',lw=1.2),transform=ax.transAxes)
 ax.text(x,.20,sub,ha='center',va='center',fontsize=8,transform=ax.transAxes)
for a,b in zip(xs[:-1],xs[1:]): ax.annotate('',xy=(b-.055,.53),xytext=(a+.055,.53),xycoords=ax.transAxes,arrowprops=dict(arrowstyle='->',lw=1.2,color='#555'))
fig.tight_layout(); fig.savefig(OUT/'framework_pipeline.png',transparent=False); plt.close(fig)
