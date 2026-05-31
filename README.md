# Steam Workshop Downloader

一个现代化的 Steam Workshop 下载工具，基于 CustomTkinter 构建，内置 DepotDownloader。

## ✨ 功能特点

- 🎨 **现代化界面** - 基于 CustomTkinter 的美观深色主题界面
- 📥 **内置 DepotDownloader** - 无需额外配置，开箱即用
- 📋 **批量下载** - 支持同时下载多个 Workshop 项目
- 🔄 **自动重试** - 下载失败自动重试机制
- ⚡ **加速下载** - 支持自定义下载节点（CM/CDN）
- 📦 **独立打包** - 提供单目录打包版本，文件完整性有保障

## 🚀 快速开始

### 方法一：直接运行打包版本

1. 下载最新发布的打包版本
2. 解压到任意目录
3. 运行 `SteamWorkshopDownloader.exe`

### 方法二：从源码运行

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python steam_workshop_modern_customtk_scrollbar_in_tree.py
```

## 📝 使用说明

1. **添加 Workshop 项目**
   - 在输入框中粘贴 Workshop 项目链接或 ID
   - 支持批量添加多个项目

2. **开始下载**
   - 点击下载按钮开始下载
   - 可以随时暂停或停止下载

3. **下载设置**（可选）
   - 下载目录：设置文件保存位置
   - 并行下载数：调整同时下载的任务数
   - 登录凭证：如需下载受限内容可配置

## 🛠️ 构建说明

### 环境要求
- Python 3.10+
- PyInstaller 6.x

### 打包命令

```bash
# 使用 onedir 模式打包（推荐）
python -m PyInstaller steam_workshop_downloader.spec

# 或直接打包
python -m PyInstaller --onedir --windowed --add-data "tools/depotdownloader;tools/depotdownloader" steam_workshop_modern_customtk_scrollbar_in_tree.py
```

## 📁 项目结构

```
steam-workshop-downloader/
├── steam_workshop_modern_customtk_scrollbar_in_tree.py  # 主程序
├── steam_workshop_downloader.spec                        # PyInstaller 配置
├── requirements.txt                                      # 依赖列表
├── run_windows.bat                                       # Windows 启动脚本
├── tools/
│   └── depotdownloader/                                  # 内置的 DepotDownloader
│       ├── DepotDownloader.exe
│       └── LICENSE
└── .gitignore                                           # Git 忽略配置
```

## 📜 许可证

本项目采用 MIT 许可证，详情请见 [LICENSE](LICENSE) 文件。

DepotDownloader 遵循其自身的许可证协议。

## 🙏 致谢

- [DepotDownloader](https://github.com/SteamRE/DepotDownloader) - Steam Workshop 下载核心
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) - 现代化 GUI 框架

---

*Made with ❤️ for Steam Workshop enthusiasts*
