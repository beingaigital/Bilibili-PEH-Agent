#!/bin/bash
# 安装代理 CA 证书到 Python certifi
# Install Proxy CA Certificate to Python certifi

set -e

echo "🔍 正在查找代理 CA 证书..."
echo ""

# 获取 certifi 证书包位置
CERTIFI_PATH=$(python3 -c "import certifi; print(certifi.where())")
echo "✓ Python certifi 证书包位置: $CERTIFI_PATH"
echo ""

# 方法 1: 从 macOS 钥匙串导出 Clash 证书
echo "方法 1: 从 macOS 钥匙串导出证书"
echo "----------------------------------------"
echo "1. 打开「钥匙串访问」应用 (Keychain Access)"
echo "2. 在左侧选择「系统」或「登录」"
echo "3. 搜索 'Clash' 或 'Verge' 或 'proxy'"
echo "4. 找到证书后，右键点击 -> 导出"
echo "5. 保存为 ~/clash-ca.crt"
echo ""

# 方法 2: 从 Clash Verge 设置导出
echo "方法 2: 从 Clash Verge 设置导出"
echo "----------------------------------------"
echo "1. 打开 Clash Verge Rev 应用"
echo "2. 进入「设置」-> 「系统代理」"
echo "3. 找到「CA 证书」或「证书」选项"
echo "4. 点击「导出证书」或「查看证书」"
echo "5. 保存为 ~/clash-ca.crt"
echo ""

# 方法 3: 手动提供证书路径
echo "方法 3: 如果你已经有证书文件"
echo "----------------------------------------"
echo "请将证书文件路径作为参数传递给此脚本："
echo "  ./install_proxy_cert.sh /path/to/your/ca.crt"
echo ""

# 检查是否提供了证书文件路径
if [ $# -eq 1 ]; then
    CERT_FILE="$1"
    if [ -f "$CERT_FILE" ]; then
        echo "✓ 找到证书文件: $CERT_FILE"
        echo ""
        echo "📝 正在安装证书到 certifi..."

        # 备份原始证书包
        if [ ! -f "${CERTIFI_PATH}.backup" ]; then
            sudo cp "$CERTIFI_PATH" "${CERTIFI_PATH}.backup"
            echo "✓ 已备份原始证书包到: ${CERTIFI_PATH}.backup"
        fi

        # 追加证书到 certifi
        sudo cat "$CERT_FILE" >> "$CERTIFI_PATH"
        echo "✓ 证书已安装到 certifi"
        echo ""

        # 验证
        echo "🧪 验证证书安装..."
        if python3 -c "import httpx; print(httpx.get('https://api.bilibili.com').status_code)" 2>/dev/null; then
            echo "✅ 证书安装成功！Bilibili API 可以访问"
        else
            echo "⚠️ 验证失败，可能需要重启 Python 进程或检查证书"
        fi
    else
        echo "❌ 错误: 证书文件不存在: $CERT_FILE"
        exit 1
    fi
elif [ -f ~/clash-ca.crt ]; then
    echo "✓ 找到证书文件: ~/clash-ca.crt"
    echo ""
    echo "📝 正在安装证书到 certifi..."

    # 备份原始证书包
    if [ ! -f "${CERTIFI_PATH}.backup" ]; then
        sudo cp "$CERTIFI_PATH" "${CERTIFI_PATH}.backup"
        echo "✓ 已备份原始证书包到: ${CERTIFI_PATH}.backup"
    fi

    # 追加证书到 certifi
    sudo cat ~/clash-ca.crt >> "$CERTIFI_PATH"
    echo "✓ 证书已安装到 certifi"
    echo ""

    # 验证
    echo "🧪 验证证书安装..."
    if python3 -c "import httpx; print(httpx.get('https://api.bilibili.com').status_code)" 2>/dev/null; then
        echo "✅ 证书安装成功！Bilibili API 可以访问"
    else
        echo "⚠️ 验证失败，可能需要重启 Python 进程或检查证书"
    fi
else
    echo "⚠️ 未找到证书文件"
    echo ""
    echo "请按照上述方法导出证书，然后重新运行此脚本："
    echo "  ./install_proxy_cert.sh ~/clash-ca.crt"
    echo ""
    echo "或者将证书保存为 ~/clash-ca.crt 后直接运行："
    echo "  ./install_proxy_cert.sh"
fi
