# 安装与维护

当前版本 **1.0.0-rc.6**，Windows本地小范围试用候选版。版本变化见 [RELEASE](RELEASE.md)。

## 全新安装与AI启用

1. 安装Python 3.12并加入PATH，解压公开发行ZIP至可写目录。
2. 双击 `run.bat`；首次建立本目录独立 `.venv`，联网安装 `requirements.lock.txt` 锁定依赖并强制核对发行文件SHA256。手动安装使用 `python -m pip install --require-hashes -r requirements.lock.txt`；`requirements.txt`引用同一份锁文件。
3. 打开 `http://127.0.0.1:8768` → 数据与设置，填写Key、勾选启用AI、保存AI配置。
4. 点击测试连接；成功后更新研究，失败原因显示在同一区域。无Key或关闭AI也可浏览已有报告。

Key在本机 `.env`，留空保存保留旧Key，系统环境变量 `DEEPSEEK_API_KEY` 优先。连接测试读取模型列表，不生成研究；浏览和打开评分浮层不调用模型。相同输入复用合格缓存，失败不自动重试；首次政策短补评与完整研究是两次同模型请求。没有每日最大请求次数，保留每日262144 tokens预算，默认单次输出32768 tokens；超过预算在设置/运行状态显示，不能把预算失败当成研究成功。

## 升级、备份、恢复、回退

先关闭程序以及本项目正在运行的定时任务。在项目目录使用PowerShell：

```powershell
.venv/Scripts/python.exe release.py backup --file backups/before-upgrade.zip
.venv/Scripts/python.exe release.py install --file C:/Downloads/market-radar-1.0.0-rc.6.zip
```

安装校验清单、路径与哈希后覆盖公共文件，保留 `.env`、`config.json`、`data/`、`reports/`；运行 `run.bat` 核对依赖。私有备份含Key和数据，只保存在自己的电脑。

安装和恢复会在写入前拒绝越出目标目录的目录联接/符号链接。哈希核验保证包内容与其清单一致，请使用自己确认来源的发行包。捕获到写入错误时，会恢复本次已改文件；恢复也失败时明确报错。断电或进程被终止时仍无法保证整包原子性；不要在中断后继续使用混合版本，应重新安装完整已验收包，必要时恢复私有备份。

```powershell
.venv/Scripts/python.exe release.py restore --file backups/before-upgrade.zip
```

回退前另存当前私有备份，然后用 `release.py install --file` 安装上一份已验收的公开包并重启，检查版本及存档。回退不改写原预测；旧版不能读取新格式时恢复其对应升级前私有备份。保留旧发行ZIP，不能只依靠当前开发目录。

历史工程清理基线：Git标签 `baseline/engineering-rc6`，提交 `6394ae8`。`artifacts/engineering-cleanup-baseline.zip` 是源码回退快照；测试、正式样本、浏览器证据在 `artifacts/engineering-cleanup/baseline/`。该目录属于本机验收资料，不进入公开包。恢复源码前先保存未提交修改，私有数据独立备份；源码回退不恢复或覆盖私有研究。

2026-10-10发布审查基线为 `aead06d` 加审查开始时的未提交修复，准确源码快照为 `artifacts/v1-audit-20261010/baseline-source.zip`，原始差异为 `baseline.patch`。关闭程序和定时任务、保存当前改动后，将快照逐文件恢复到项目目录，并移走本轮新增且不在快照中的 `tests/test_cli_status.py`，即可回退本轮源码；快照不包含私有配置、行情或预测。分批差异和日志保存在同一审查目录。不要使用会清除未提交改动或私有文件的强制重置/清理命令。

## 自动运行

设置页启用后，Windows任务每5分钟检查。北京时间交易日18:00盘后、08:30开始盘前检查；09:15前成功完成才可采用盘前研究。建议08:25前开机、联网并保持登录。`scheduler.py` 使用已核验2026年日历；未知年度暂停相关调度。失败保留上次研究，不自动重复模型请求。

## 故障定位

| 症状 | 查看及处理 |
|---|---|
| 启动/依赖失败 | `run.bat` 的提示，Python版本及本目录 `.venv`；按锁定依赖重装，勿借用开发目录模块 |
| 数据更新未完成 | `data/last_run.json` 的readiness、`data/checkpoint.json` 的任务状态/原始原因；续采复用已合格项 |
| AI未启用/连接失败 | 设置页启用状态、模型配置、测试连接反馈；核对系统环境变量与本机Key，勿将Key贴入日志 |
| AI校验失败 | `data/ai/` 的原响应/输入/业务状态及逐日 `ai-review-attempt.json`；保留原文，显式重试前检查费用 |
| 页面仍显示上次报告 | 核对行情日期与生成时点；必需数据缺失时保留上次正式报告是既有行为 |
| JSON损坏或无法读取 | 页面说明具体文件；从对应私有备份恢复，勿手改历史预测或以空对象覆盖证据 |
| 复盘缺数据/待到期 | 核对实际后续交易日与行情，不补零、不提前判断输赢 |

调试入口与数据位置见 [ARCHITECTURE](ARCHITECTURE.md)，完整发布验收要求见 [VALIDATION](VALIDATION.md)。
