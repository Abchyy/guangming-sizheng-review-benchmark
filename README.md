# 文本稿件思政审校 benchmark v0.1

build-eval 交付包。16 条合成种子与 16 条设计者候选答案均待复核；官方候选出处未完成全文核验。当前没有系统基线或实测分数，未开展 hillclimb。

公开仓库的材料范围及原始项目介绍的处理方式见 [来源说明](来源说明.md)。

阅读顺序：[方案](方案.md) → [评分规则](评分规则.md) → [标注规范](标注规范.md) → [接口规范](接口规范.md) → [资料清单](资料清单.md)。标注模板位于 `templates/标注模板.json`。种子只用于公开开发资料；`splits/proposed.json` 是按来源生成的候选分配，不能作为独立最终测试。

## 离线工具

在本目录执行，Python 3 标准库即可：

```bash
python3 scripts/bench.py validate --cases data/seeds.jsonl --sources data/sources.jsonl --split-manifest splits/proposed.json
python3 scripts/tool_selfcheck.py
```

上述只验证结构、来源分组和评分器逻辑，不调用被测系统。自检包含误报、重复报警、虚构出处、错误跨度、缺失输出、人工评分 hash 过期和运行失败处理；没有把候选答案当成真实模型运行。

需要重新生成草案数据或候选拆分时（会覆盖相应草案文件，重新生成后更新指纹）：

```bash
python3 scripts/generate_seeds.py
python3 scripts/bench.py split --cases data/seeds.jsonl --sources data/sources.jsonl --out splits/proposed.json
```

仅检查评分流程的显式示例（输出是合成候选的机械诊断，综合分保持 null，不能报告为基线）：

```bash
python3 scripts/bench.py score --cases data/seeds.jsonl --sources data/sources.jsonl --predictions data/candidate_answers.jsonl --allow-pending --out results/pipeline-only.json
```

默认 score 不接受待复核标签。source ID／片段存在不证明权威真实性；修改建议质量须人工审定。每条人工评分绑定输出 SHA-256，修改输出后必须重新评分。

## 未来系统接入

获得接口和预算授权后，复制 `config/adapter.example.json`，填写真实 adapter、cwd、版本和知识库快照。adapter stdin/stdout 协议与 eval-hillclimb 相同；本包使用自定义采集器保留输出和错误，再用多维评分器与人工评分完成评测。没有默认字符串相等 grader，不需要调用通用 hillclimb 比较器。

```bash
python3 scripts/run_adapter.py --cases approved/dev.jsonl --sources approved/sources.jsonl --config config/real-adapter.json --out results/baseline-predictions.jsonl
python3 scripts/bench.py score --cases approved/dev.jsonl --sources approved/sources.jsonl --predictions results/baseline-predictions.jsonl --manual approved/output-reviews.jsonl --out results/baseline-report.json
```

`approved/` 和真实配置当前不存在，以上为接入规范。collector 对未替换占位入口直接拒绝执行；不会假装运行。用重复调用衡量稳定性时，不把调用数当独立样本数。collector 输出元数据含源码、数据、配置和适配器文件 hash；远端服务、动态依赖、知识库与人工评分仍需单独存档。

## 指纹和有效性

```bash
python3 scripts/bench.py freeze --root . --source-document ../光明思政大模型-项目介绍.md --out manifest.json
```

manifest 仅冻结本次草案文件内容，不证明标准答案正确或实现了严格盲测。本包由同一代理设计样本、候选答案和评分器，是合成且非独立的评测准备。接入真实系统、补齐专家标签和独立来源测试后，才可报告审校能力；任何未来优化另行授权，不属于本次 build-eval。
