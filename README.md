# FITS Cutout Service

天文巡检平台的小窗口提取服务：从原始 FITS 主图像中截取窗口，按头部标定值
（BSCALE / BZERO / BLANK）返回可比较的物理量，避免字节序、空值与十进制缩放
差异污染质控结论。

## 运行

```bash
cp .env.example .env          # 可选，修改 API_PORT 配置宿主机端口
docker compose up -d api      # 启动 API（默认宿主机端口 8080）
```

- 宿主机端口由环境变量 `API_PORT` 决定（默认 8080），容器内固定 8000。
- 健康检查：`GET /health`（Compose healthcheck 亦使用它）。

## 一次性验证

`verify` 服务等待 API 健康后依次执行：单元测试（pytest）→ 构建检查
（字节码编译）→ FITS 接口冒烟，并以退出码报告结果（全过为 0）：

```bash
docker compose up --exit-code-from verify   # 退出码即 verify 结果
# 或
docker compose run --rm verify
```

## API

### `POST /api/fits/cutout`

- 请求体：`Content-Type: application/fits`，单个 FITS 文件，不超过 16 MiB。
- 查询参数（均为零基整数）：`x`、`y`（窗口原点），`width`、`height`（窗口尺寸）。
- 窗口不得越界，且 `width*height ≤ 10000`。

成功响应 `200`：

```json
{
  "x": 2,
  "y": 3,
  "width": 4,
  "height": 2,
  "sha256": "<原文件的 SHA-256>",
  "pixels": [[null, "-6.7", "-6.6", "-6.5"], ["-5.8", "-5.7", "-5.6", "-5.5"]]
}
```

- 像素按 FITS 大端有符号整数解释，按图像行序（逐行、每行从左到右）输出。
- 命中 `BLANK` 的像素输出 `null`；其余精确应用可选 `BSCALE`/`BZERO`
  （十进制精确运算，非二进制浮点），以**无指数、无多余尾零**的十进制
  字符串输出。

### 错误（均为 4xx，不产生部分结果）

| 条件 | 状态码 |
| --- | --- |
| Content-Type 非 application/fits | 415 |
| 文件超过 16 MiB | 413 |
| 结构损坏（卡片/END/对齐/轴长/数据长度/尾部内容）、BLANK 越界、BSCALE/BZERO 非有限、窗口非法 | 400 |
| 查询参数缺失或非整数 | 422 |

## FITS 接受范围

仅接受单一主 HDU：`SIMPLE = T`、`NAXIS = 2`、`BITPIX ∈ {16, 32}`。校验
80 字符卡片（可打印 ASCII、关键字合法、`= ` 值指示符）、必备关键字顺序、
END 卡片及其后空白、2880 字节对齐、轴长度为正、数据长度精确、主 HDU 之后
无尾部内容；`PCOUNT` 须为 0、`GCOUNT` 须为 1，结构关键字不得重复。

## 本地开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest                      # 单元测试
uvicorn app.main:app --port 8080 &
API_BASE_URL=http://127.0.0.1:8080 python -m verify.smoke   # 冒烟
```

## 布局

```
app/fits.py     严格 FITS 主 HDU 解析、窗口校验、十进制格式化与像素提取
app/main.py     FastAPI 入口（/health、/api/fits/cutout、16 MiB 限制）
tests/          pytest 单元测试（解析器 + API）
verify/         一次性验证：fitsbuild 样例构造、smoke 冒烟、run 编排
Dockerfile      API 镜像（verify 复用同一镜像）
docker-compose.yml  api（健康检查、API_PORT 宿主机端口）+ verify（一次性）
```
