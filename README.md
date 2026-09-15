# Screen List Capturer

**零爬虫做竞品数据抓取**

**问题场景
**做网球约球小程序时，需采集网球场地信息。公开的地图api提供商并无网球场相关的详细信息，从竞品逐个人工收集成本极高。

**设计思路：
**屏幕上的列表  ->  自动化截图 ->  pages/page_001.png ...  ->  pages.zip + LLM_PROMPT.md  ->  CSV  

```

## 演示
> **[▶ Demo page](https://m2290526022-boop.github.io/screen-list-capturer/)**
> | [download MP4 (7.7 MB)](https://github.com/m2290526022-boop/screen-list-capturer/blob/main/docs/demo.mp4)

在 macOS 上对着一个没有接口的小程序列表实录（1 分 09 秒）。画面里能看到：标定区域 →
自动滚动、逐屏截图 → 工具判断列表已经不再移动，**自己停下来**，而不是对着底部那一帧一直拍。

## 工作原理

```
1. **标定** —— 倒计时引导你点出列表的左上角和右下角。屏幕上其他东西都不影响。
2. **截图** —— 工具滚动、等列表稳定，然后每屏存一张 PNG 到 `pages/`。列表不再变化时自动停止。
3. **打包** —— 截图打包成 zip，同时生成 `LLM_PROMPT.md`：告诉模型该抽哪些字段、什么不许编。
   提示词会一并复制到剪贴板。
4. **抽取** —— 把 zip 和提示词交给任意支持视觉的模型，它返回 CSV。

## 安装

```bash
git clone https://github.com/m2290526022-boop/screen-list-capturer.git
cd screen-list-capturer
pip install -r requirements.txt
python screen_list_capturer.py
```

需要 Python 3.9+。`tkinter` 一般随 Python 自带（若缺失见 `requirements.txt` 注释）。

### macOS 权限

工具要驱动真实鼠标、读取真实屏幕，所以需要给终端（或你用的 Python 启动器）授权：

- **系统设置 → 隐私与安全性 → 辅助功能**
- **系统设置 → 隐私与安全性 → 屏幕录制**

Retina / HiDPI 屏无需任何设置：截图区域用逻辑点（和鼠标坐标同一套单位），
截图本身会以 2 倍分辨率返回。

### Windows / Linux

开箱可用。Linux 下 `ImageGrab` 可能需要 `scrot` 或 `gnome-screenshot`。


## 配置项

| 想改什么 | 去哪改 |
|---|---|
| 区域、重叠百分比、翻页方式 | 工具界面顶部 |
| 抽取字段、提示词措辞 | `prompt_template.md`，随便改，打包时只替换 `{n_pages}` |
| 产物位置 | 脚本同级的 `pages/`、`pages.zip`、`LLM_PROMPT.md` |

重叠率很关键：重叠越大页数越多（token 越贵），但跨页被截断的条目越少。默认 50% 比较稳。

`prompt_template.md` 就是全部的抽取契约 —— 把字段列表换成你自己的场景，其他什么都不用改。


## 使用声明

这是一个通用屏幕截图工具。请只用于你自己的屏幕、自己的账号、以及你有权采集的数据。
采集第三方 App 或网站前，请先确认其服务条款与适用法律，不要用产出去构建竞品数据集。
作者不对使用方式负责。

## License

[MIT](./LICENSE)
