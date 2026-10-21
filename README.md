# ASR Bridge

将微信桌面端、微信输入法和豆包网页版语音识别整合成一个 Windows 应用及 OpenAI 兼容的本机音频转写接口。音频发送到所选后端的服务端，不发送给 OpenAI。

[English](README.en.md) · [MIT License](LICENSE) · [接口文档](docs/api.html) · [窗口版使用说明](docs/PORTABLE.md)

## 窗口版

使用与源码版本对应的 `WeChatASRBridge-0.3.3-windows-x64-portable.zip`，解压整个文件夹并运行 `WeChatASRBridge.exe`。本地构建产物在 `dist/`；GitHub Releases 的最新正式发布版本可能早于当前源码。

- **首页**：服务状态、三个独立并发池、Base URL、model 和接口文档入口。每个小格代表一个并发名额，占用显示绿色，空闲无填色。
- **手动转写**：选择 model、选择音频或示例音频、识别、复制或保存文字。
- **设置**：端口、本地 API Key 和三个后端各自的并发数。每个模型默认并发 4，可分别设置正整数；各模型独立调度，超出本池并发数的请求排队。首页显示总占用和各池占用。这些配置不是上游稳定性保证，旧版已保存的自定义值会保留。
- 关闭窗口后驻留托盘；正常退出会等待请求和原生会话清理。只停止本应用启动的服务。

![窗口版首页](docs/portable-preview.png)

## 模型路由

| model | 后端 | 条件 |
|---|---|---|
| `wechat-voice` | 微信桌面端 | 微信已登录，账号的 iLink/Opus 能力可用；参考版本 4.1.13.65 |
| `wetype-voice` | 微信输入法 | 输入法正在运行；已验证 3.0.0.34、3.0.0.37 |
| `doubao-voice` | 豆包网页版匿名会话 | 能正常访问豆包网页及语音网关，无需浏览器登录 |

微信和输入法的版本或模块指纹不匹配只产生警告，不再据此拒绝调用。输入法按函数特征自动定位编码器。警告可在响应 JSON 的 `warnings`、`x-asr-warnings` 响应头、`/health` 和窗口「诊断详情」中查看。某个后端不可用时，其他后端仍可使用。豆包使用普通游客初始化，不读取 Chrome/Edge 登录信息，也不保存浏览器 Cookie。

实际调用失败仍返回对应 HTTP 错误码，并包含具体 `error.message`、`error.code`、底层异常 `error.details` 和请求 ID。窗口的手动转写结果区显示完整错误，首页「诊断详情」可查看并复制后台请求的错误与警告。

原生客户端与私有网页协议可能变化，当前实现不能保证任意版本或长期稳定运行。自定义并发数不会提高上游限额。大并发池的格子分组显示，显示分页不会减少实际配置容量。

## 从源码运行

Windows x64，使用 Python 3.11 conda 环境：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
.\env\python.exe desktop.py
```

仅启动 HTTP 服务：

```powershell
.\env\python.exe server.py --port 8765 `
  --wechat-concurrency 4 --wetype-concurrency 4 --doubao-concurrency 4
```

也可使用 `start.ps1 -Background`；三个并发参数分别为 `-WechatConcurrency`、`-WetypeConcurrency`、`-DoubaoConcurrency`。停止使用 `stop.ps1 -Port 8765`。单后端诊断可使用 `server.py --backend wechat|wetype|doubao`；单后端同样使用对应的正式 model 名称。

## 接入其他程序

| 参数 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8765/v1` |
| model | 上表中的路由标识 |
| API Key | 未启用本地鉴权时任意值或留空均被忽略；启用后填设置中的密钥 |
| 转写 | `POST /v1/audio/transcriptions`，multipart 表单 |
| 模型列表 | `GET /v1/models` |
| 状态及并发池 | `GET /health`，只读，不提交识别 |
| HTML 接口文档 | `http://127.0.0.1:8765/docs`，也可离线打开 `docs/api.html` |

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="local",
                timeout=180, max_retries=0)
with open("audio.wav", "rb") as audio:
    result = client.audio.transcriptions.create(
        model="doubao-voice", file=audio, response_format="json")
print(result.text)
```

单个上传文件最大 25 MiB，完整解码后在内存中转写。支持 `json` 和 UTF-8 `text`；`stream=false`、`temperature=0` 仅作为兼容默认值。`language`、`prompt`、字幕、逐词时间戳、说话人分离及流式响应尚未实现，传入时明确返回错误。

默认只监听 `127.0.0.1`。使用 `--host` 暴露非本机地址前必须设置 `WECHAT_API_KEY`。在设置中勾选「启用 API Key 鉴权」才会要求密钥；关闭时，客户端任意 Key 或不传 Key 都被忽略，修改闲置 Key 不会重启服务。旧版已设置密钥的安装会保留鉴权。窗口保存的密钥使用当前用户 Windows DPAPI 加密，不会作为命令行参数传给子进程。接口文档不包含密钥。

## 开发、构建和验证

```powershell
.\env\python.exe -m unittest discover -s tests -p "test_*.py" -v
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

打包程序包含三个后端、两份原生 agent、FFmpeg、HTML 文档及合成示例音频；不包含微信/输入法程序、认证凭据、个人录音、运行日志或本地素材。构建生成免安装目录、ZIP、第三方许可和 SHA-256 文件。

离线测试覆盖模型路由、独立队列、每池默认 4 路与独立并发设置、排队取消清理、回调隔离、错误释放、配置持久化及 96/120/144/192 DPI 两页窗口布局。离线假后端并发通过不代表真实上游有相同吞吐量。打包程序另有 `--self-test`、`--self-test-lifecycle` 检查运行依赖、托盘、鉴权、服务重启与退出。

仅在要把仓库合成音频发送给相应服务时运行真实验证：

```powershell
.\env\python.exe tests\verify_unified_live.py
```

既有微信专项验证仍在 `tests/verify_live.py`、`verify_long.py`、`verify_stability.py`；默认并发假设为单后端微信模式。输入法和豆包协议的背景及历史验证见 [WETYPE.md](docs/WETYPE.md)、[豆包实验记录](experiments/doubao_asr/VERIFICATION.md)。

本轮验证见 [v0.3.3 版本警告与错误诊断验证](docs/VERSION_WARNING_VERIFICATION.md)。历史验证范围见 [v0.3.2 界面与调度验证](docs/UI_CONCURRENCY_VERIFICATION.md) 和 [v0.3.0 整合验证记录](docs/UNIFIED_VERIFICATION.md)。

项目与腾讯、微信、字节跳动和 OpenAI 无隶属关系。
