#!/bin/bash
# ========================================
# Kalm Startup Script
# ========================================
# Auto-detect Python environment and start Kalm API service
# Support custom host and port
#
# Usage:
#   ./run.sh
#   ./run.sh --port 8000
#   ./run.sh --host 127.0.0.1 --port 8000
# ========================================

# Default parameters (empty = use config.yaml defaults)
HOST_ADDRESS=""
PORT=""

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --host)
            HOST_ADDRESS="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --install-deps)
            INSTALL_DEPS=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: ./run.sh [--host <host>] [--port <port>] [--install-deps]"
            exit 1
            ;;
    esac
done

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Color definitions
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  Kalm Startup Script${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

# ---- Python 环境检测 ----
# 项目使用 conda 管理环境，环境目录可能是: .venv, venv, python3.11, python3.12 等
# 这些目录是 conda 在项目目录下创建的环境，内部有可直接执行的 python
# 检测优先级：本地 conda 环境目录 > conda 命名环境 > 系统 python
PYTHON_CMD=""

# 候选环境目录名称列表（按优先级排序）
CANDIDATE_ENVS=(".venv" "venv")
# 同时查找 python3.x 格式的目录
for py_dir in "$SCRIPT_DIR"/python3.[0-9]*; do
    if [ -d "$py_dir" ]; then
        dir_name=$(basename "$py_dir")
        CANDIDATE_ENVS+=("$dir_name")
    fi
done

# 第一步：检查本地 conda 环境目录（直接执行 python）
for env_name in "${CANDIDATE_ENVS[@]}"; do
    ENV_PATH="$SCRIPT_DIR/$env_name"
    if [ ! -d "$ENV_PATH" ]; then
        continue
    fi

    # 尝试多种 python 路径
    for py_bin in "bin/python" "bin/python3" "Scripts/python" "python"; do
        PYTHON_EXE="$ENV_PATH/$py_bin"
        if [ -x "$PYTHON_EXE" ]; then
            echo -e "${GREEN}[INFO] Found conda environment directory: $env_name${NC}"
            echo -e "${GREEN}[INFO] Using Python: $PYTHON_EXE${NC}"
            PYTHON_CMD="$PYTHON_EXE"
            break 2
        fi
    done
done

# 第二步：如果本地目录没有可用 python，尝试 conda activate 命名环境
if [ -z "$PYTHON_CMD" ]; then
    CONDA_AVAILABLE=false
    if command -v conda &> /dev/null; then
        CONDA_AVAILABLE=true
    fi

    if [ "$CONDA_AVAILABLE" = true ]; then
        # 把候选名 + "kalm" 作为 conda 环境名尝试
        ALL_ENV_NAMES=("${CANDIDATE_ENVS[@]}" "kalm")
        # 去重
        ALL_ENV_NAMES=($(echo "${ALL_ENV_NAMES[@]}" | tr ' ' '\n' | sort -u | tr '\n' ' '))

        for env_name in "${ALL_ENV_NAMES[@]}"; do
            # 检查该 conda 环境是否存在
            ENV_EXISTS=$(conda env list 2>/dev/null | grep "^${env_name} ")
            if [ -n "$ENV_EXISTS" ]; then
                echo -e "${YELLOW}[INFO] Activating conda environment: $env_name...${NC}"

                CONDA_SH=$(find ~/.conda ~/anaconda3 ~/miniconda3 /opt/conda /opt/anaconda3 -name "conda.sh" 2>/dev/null | head -1)
                if [ -n "$CONDA_SH" ]; then
                    source "$CONDA_SH" activate "$env_name" 2>/dev/null
                else
                    eval "$(conda shell.bash hook)" 2>/dev/null
                    conda activate "$env_name" 2>/dev/null
                fi

                if [ $? -eq 0 ]; then
                    PYTHON_CMD="python"
                    echo -e "${GREEN}[SUCCESS] Conda environment '$env_name' activated${NC}"
                    echo -e "${GREEN}[INFO] Python: $(python --version 2>&1)${NC}"
                    break
                else
                    echo -e "${YELLOW}[WARNING] Failed to activate conda environment '$env_name'${NC}"
                fi
            fi
        done
    fi
fi

# 第三步：回退到系统 python
if [ -z "$PYTHON_CMD" ]; then
    if [ "${CONDA_AVAILABLE:-false}" = true ]; then
        echo -e "${YELLOW}[INFO] No matching conda environment found, trying system Python...${NC}"
    fi

    if command -v python3 &> /dev/null; then
        echo -e "${YELLOW}[INFO] Using system Python3: $(python3 --version 2>&1)${NC}"
        PYTHON_CMD="python3"
    elif command -v python &> /dev/null; then
        echo -e "${YELLOW}[INFO] Using system Python: $(python --version 2>&1)${NC}"
        PYTHON_CMD="python"
    else
        echo -e "${RED}[ERROR] No Python found on the system${NC}"
        read -p "Press Enter to exit..."
        exit 1
    fi
fi

echo ""

# ---- 检查 main.py ----
if [ ! -f "$SCRIPT_DIR/main.py" ]; then
    echo -e "${RED}[ERROR] main.py not found in $SCRIPT_DIR${NC}"
    read -p "Press Enter to exit..."
    exit 1
fi

# ---- 检查并安装依赖 ----
INSTALL_DEPS="${INSTALL_DEPS:-false}"

if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    # 检查关键依赖是否已安装
    MISSING_DEPS=$($PYTHON_CMD -c "import uvicorn, fastapi, yaml" 2>&1)
    if [ -n "$MISSING_DEPS" ]; then
        echo -e "${YELLOW}[WARNING] Some dependencies may be missing:${NC}"
        echo -e "  $MISSING_DEPS"
        echo ""
        read -p "Install dependencies from requirements.txt? (y/N): " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            INSTALL_DEPS=true
        else
            echo -e "${RED}[ERROR] Dependencies not installed, cannot start service${NC}"
            read -p "Press Enter to exit..."
            exit 1
        fi
    fi
fi

if [ "$INSTALL_DEPS" = true ]; then
    echo -e "${CYAN}[INFO] Installing dependencies...${NC}"
    $PYTHON_CMD -m pip install -r "$SCRIPT_DIR/requirements.txt"
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}[SUCCESS] Dependencies installed${NC}"
    else
        echo -e "${RED}[ERROR] Failed to install dependencies${NC}"
        read -p "Press Enter to exit..."
        exit 1
    fi
    echo ""
fi

# ---- 自定义参数：临时修改 config.yaml ----
CONFIG_MODIFIED=false
ORIGINAL_CONFIG=""

# 只有用户通过命令行参数指定了 host/port 时才修改配置
if [ -n "$HOST_ADDRESS" ] || [ -n "$PORT" ]; then
    echo -e "${YELLOW}[INFO] Custom parameters detected, updating config...${NC}"
    
    if [ -f "$SCRIPT_DIR/config.yaml" ]; then
        # 备份原始配置
        ORIGINAL_CONFIG=$(cat "$SCRIPT_DIR/config.yaml")
        
        if [ -n "$HOST_ADDRESS" ]; then
            sed -i '/^api_server:/,/^[a-zA-Z]/ s/^[[:space:]]*host:.*/  host: '"$HOST_ADDRESS"'/' "$SCRIPT_DIR/config.yaml"
            echo -e "${CYAN}  - Set Host: $HOST_ADDRESS${NC}"
        fi
        
        if [ -n "$PORT" ]; then
            sed -i '/^api_server:/,/^[a-zA-Z]/ s/^[[:space:]]*port:.*/  port: '"$PORT"'/' "$SCRIPT_DIR/config.yaml"
            echo -e "${CYAN}  - Set Port: $PORT${NC}"
        fi
        
        CONFIG_MODIFIED=true
    else
        echo -e "${YELLOW}[WARNING] config.yaml not found, using default config${NC}"
    fi
fi

echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  Starting Kalm service...${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

# ---- 清理函数：退出时恢复配置 ----
cleanup() {
    if [ "$CONFIG_MODIFIED" = true ] && [ -n "$ORIGINAL_CONFIG" ]; then
        echo ""
        echo -e "${YELLOW}[INFO] Restoring original config...${NC}"
        echo "$ORIGINAL_CONFIG" > "$SCRIPT_DIR/config.yaml"
        echo -e "${GREEN}[SUCCESS] Config restored${NC}"
    fi
    echo ""
    echo -e "${CYAN}========================================${NC}"
    echo -e "${CYAN}  Kalm service stopped${NC}"
    echo -e "${CYAN}========================================${NC}"
}

# 注册清理函数
trap cleanup EXIT

# ---- 启动服务 ----
$PYTHON_CMD "$SCRIPT_DIR/main.py"
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo -e "${RED}[ERROR] Service exited with code $EXIT_CODE${NC}"
fi

exit $EXIT_CODE