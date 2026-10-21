# 微信输入法语音接口（实验）

本分支增加 Windows 微信输入法 x64 后端，调用输入法进程内已初始化的业务网络层。已验证 3.0.0.34 与 3.0.0.37。统一窗口版通过 model 切换后端，也可通过源码命令行单独启动。

## 启动与调用

先启动微信输入法，确保其语音功能可正常联网。使用项目的 Python 3.11 Conda 环境及 `requirements.txt`：

```powershell
.\env\python.exe .\server.py --backend wetype --port 8766
```

多个 `wetype_update.exe` 并存时，用 `--pid <PID>` 或 `WETYPE_PID` 指定。此后端的 PID 指向输入法进程。连接时检查必要模块，并按函数特征定位编码器，不再按版本号或完整文件 SHA-256 拒绝原生调用。

| 配置 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:8766/v1` |
| 模型别名 | `wetype-voice` |
| API Key | 未启用鉴权时任意值或不传均忽略 |
| 输出格式 | JSON 或纯文本 |
| 并发 | 默认本池 4 路，独立调度，超出排队 |
| 上传大小 | 最多 25 MiB |

```python
from openai import OpenAI, DefaultHttpxClient

client = OpenAI(
    base_url="http://127.0.0.1:8766/v1",
    api_key="local",
    timeout=180,
    max_retries=0,
    http_client=DefaultHttpxClient(trust_env=False),
)
with open("tests/fixtures/test.wav", "rb") as audio:
    result = client.audio.transcriptions.create(model="wetype-voice", file=audio)
print(result.text)
```

接口提供 `GET /v1/models`、`POST /v1/audio/transcriptions` 和 `GET /health`。WAV、MP3 等音频通过现有解码路径转为 PCM16。暂不支持流式 HTTP、时间戳或说话人分离。

识别请求由输入法发送到其服务端，音频不发送给 OpenAI。`wetype-voice` 是本地路由名称，不代表上游模型身份。这个后端不需要微信聊天窗口进程运行，仍需输入法自己的网络与认证状态可用；本实现没有恢复或绕过认证。

## 原生实现

```text
HTTP -> 音频解码 -> 必要模块检查、指纹差异警告及编码器地址定位
     -> wetype_agent.js
     -> 独立 TROpusTool 编码对象，16 kHz/mono/64 kbps
     -> #!OPUS_RAW_V1 音频流，按完整帧拆成最多 5 秒分片
     -> Python 组装 ImeVoiceAssistantReq + ImeVoiceTranslateReq
     -> business_net_start_task_with_option，命令 4548
     -> 独立回调 + 会话标识校验 -> 结束分片 + 轮询
     -> 明确的服务端 end_flag -> 文字
```

模块 RVA 只适用于核验过的样本。编码使用独立对象；上传文件不经过输入法麦克风采集或文本上屏流程，不会把识别结果输入当前应用。桥接器不读取聊天数据库或已有个人录音，不修改输入法设置。

每次请求使用独立会话 ID，持续复用进程级 agent。每个音频分片携带连续序号及累计字节数，流头只出现在第一个分片。发送结束分片后通过序号 0 的轮询请求获取最终结果。

**成功必须收到已知的服务端结束标志。** 单独有文字、上传序号被确认，或文字连续稳定，都不算识别完成。超时返回 504，不交付中途文字。响应头 `x-asr-completion: server_end_flag` 和 `/health` 的 `completion` 可以检查完成方式。

### 可选文字处理开关

本接口返回 ASR 文字，不主动启用自动分段、去冗余或强化润色。早期探针强制设置请求字段 22 时，短音频能返回文字，但长音频出现错序且不置位结束标志；减小分片并按实时时钟发送没有解决。取消该选项后，测试重新得到正确顺序与明确结束标志。因此最终实现不包含这组未完整适配的可选开关，也没有“文字稳定即完成”的兜底。

构造函数与序列化对照修正了初步静态报告的部分推断：分段、去冗余、强化润色的写入对应字段 22、23、24，应用信息对应字段 25。固定字段值 5 与音频格式路径相关，不是模型编号。

## 回调所有权与停止

编码完成后释放独立编码器。网络请求的 JS 回调和缓冲区持续保留到异步回调返回。请求超时不会释放仍可能被原生代码引用的回调。

清理状态不确定时，暂停后续识别并保留 agent。停止服务时也不会卸载仍有原生请求引用的脚本，此时需要等待输入法进程退出。不要把强杀 Python 当作正常停止方式。

这部分有隔离测试覆盖晚到回调、超时、停止和请求间所有权；没有在真实输入法中进行断网或强杀等故障注入，不能声称所有真实故障路径已验证。

## 验证与边界

```powershell
.\env\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\env\python.exe -m compileall -q backend.py server.py wetype_backend.py wetype_protocol.py tests
```

先启动一个全新的输入法测试服务，再运行真实 SDK 检查：

```powershell
.\env\python.exe .\tests\verify_wetype.py --base-url http://127.0.0.1:8766/v1
```

脚本验证模型列表、WAV/MP3、JSON/纯文本、双请求排队，以及约 50 秒音频中每段顺序与独立结尾；只忽略标点和空白差异，不忽略词语缺失、重复或乱序。运行记录在 `.runtime/`，不提交到 Git。

2026-10-15 本机 SDK 复验通过：6.28 秒 WAV、约 5.76 秒 MP3、两路请求在单槽位排队、49.70 秒合成音频的完整重复段落和独立结尾。每个成功响应均确认 `server_end_flag`。另外验证了 36.20 秒多段合成语音的段落顺序，并在测试服务卸载后重新连接、识别、卸载两次；目标进程保持同一 PID。这些都是有限样本结果，不代表任意音频识别准确率保证。

2026-10-15 的早期内部请求对象构造探针曾导致输入法进程退出并自动重启；该路径已弃用，未进入实现。最终代码在 Python 中显式编码 wire 数据。有限复验不代表长期运行稳定性已被证明；识别准确率仍由输入法服务决定。

当前仅适配一个输入法版本；不包含腾讯安装文件、认证密钥、用户数据库，也没有恢复模型名称、权重或部署标识，不能据此确认它和微信聊天框使用同一个模型。

## 当前统一服务

统一服务支持 model=wetype-voice；并发预算在桌面设置或 --wetype-concurrency 中指定。默认 4 路，按本池设置独立调度，不通过 Python 内部锁强制串行。每条音频具有独立会话 ID 和回调所有权，finalize 只检查对应请求；其他请求的回调未完成时不会误判本请求清理失败。真实并发能力仍须按环境验证。
## 输入法升级后的兼容定位

输入法后端不再按完整文件 SHA-256 或版本号拒绝启动，差异会以 `unverified_wetype_version` 警告交付。加载原生 agent 前，在 x64 PE 的可执行节中查找四个编码函数的唯一特征，并使用实际地址；实际无法定位函数时返回 `wetype_encoder_unavailable` 及具体原因。已在本机 3.0.0.37 上完成真实合成音频识别验证。这不意味着任意未来版本都保持原生 ABI 兼容。
