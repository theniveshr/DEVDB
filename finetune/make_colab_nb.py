"""Script to generate finetune/train_colab.ipynb with valid JSON structure."""

import sys
import json
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
NOTEBOOK_FILE = BASE_DIR / "train_colab.ipynb"

cells = [
    # 0. Title & Introduction
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "# 🚀 DevDB Assistant: Fine-Tuning Llama 3.2 3B Instruct with Unsloth\n",
            "\n",
            "This notebook guides you through fine-tuning **Llama 3.2 3B Instruct** on your custom **DevDB database assistant dataset** using **Unsloth** and **LoRA** (Low-Rank Adaptation).\n",
            "\n",
            "### 🎯 Key Highlights:\n",
            "- **Hardware**: Runs comfortably on a **free Google Colab T4 GPU** (~15 GB VRAM).\n",
            "- **Speed**: Unsloth provides 2x to 5x faster training with up to 70% less memory usage.\n",
            "- **Data**: Uses the `train.jsonl` (85%) and `val.jsonl` (15%) created in Phase 12B.\n",
            "- **Loss Masking**: Trains **only** on the assistant's JSON answers (`train_on_responses_only`), keeping prompt and schema conditioning intact.\n",
            "- **Export**: Automatically quantizes and exports a merged **GGUF** file (`devdb-llama.Q4_K_M.gguf`) ready to be imported into your local Ollama runtime!\n",
            "\n",
            "> ⚠️ **Important Colab Setting**:\n",
            "> Go to **Runtime** ➔ **Change runtime type** ➔ select **T4 GPU** ➔ click **Save** before running!"
        ]
    },
    # 1. Install Unsloth
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## 📦 Cell 1: Install Unsloth and Required Libraries\n",
            "\n",
            "**What this cell does:**\n",
            "Installs the official Unsloth package optimized for Google Colab, along with Hugging Face `trl` (Transformer Reinforcement Learning), `peft` (Parameter-Efficient Fine-Tuning), `datasets`, and `bitsandbytes` (for 4-bit quantization).\n",
            "\n",
            "Click the **Play** button to install."
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "%%capture\n",
            "# Official Unsloth Colab install command\n",
            "!pip install \"unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git\"\n",
            "!pip install --no-deps \"xformers<0.0.29\" \"trl<0.9.0\" peft accelerate bitsandbytes\n",
            "!pip install datasets matplotlib\n",
            "\n",
            "import torch\n",
            "print(f\"✓ PyTorch version: {torch.__version__}\")\n",
            "print(f\"✓ CUDA available: {torch.cuda.is_available()}\")\n",
            "if torch.cuda.is_available():\n",
            "    print(f\"✓ Active GPU: {torch.cuda.get_device_name(0)}\")\n"
        ]
    },
    # 2. Upload Datasets
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## 📂 Cell 2: Upload `train.jsonl` and `val.jsonl`\n",
            "\n",
            "**What this cell does:**\n",
            "Prompts you to upload the dataset files created in Phase 12B from your local project folder:\n",
            "- `finetune/dataset/train.jsonl` (~850-900 examples)\n",
            "- `finetune/dataset/val.jsonl` (~150 examples)\n",
            "\n",
            "*Alternative*: If you prefer using Google Drive, uncomment the drive mount lines."
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "import os\n",
            "from google.colab import files\n",
            "\n",
            "# OPTION A: Direct Upload via browser\n",
            "print(\"Please upload 'train.jsonl' and 'val.jsonl' from your computer:\")\n",
            "uploaded = files.upload()\n",
            "\n",
            "# OPTION B (Optional): Mount Google Drive if files are stored there\n",
            "# from google.colab import drive\n",
            "# drive.mount('/content/drive')\n",
            "# !cp /content/drive/MyDrive/devdb/train.jsonl .\n",
            "# !cp /content/drive/MyDrive/devdb/val.jsonl .\n",
            "\n",
            "# Verification check\n",
            "assert os.path.exists(\"train.jsonl\"), \"❌ Missing train.jsonl! Please upload train.jsonl.\"\n",
            "assert os.path.exists(\"val.jsonl\"), \"❌ Missing val.jsonl! Please upload val.jsonl.\"\n",
            "\n",
            "train_size = sum(1 for _ in open(\"train.jsonl\", encoding=\"utf-8\"))\n",
            "val_size = sum(1 for _ in open(\"val.jsonl\", encoding=\"utf-8\"))\n",
            "print(f\"✓ Files uploaded successfully! Train: {train_size} examples | Val: {val_size} examples.\")\n"
        ]
    },
    # 3. Load Model
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## 🧠 Cell 3: Load Llama 3.2 3B Instruct in 4-bit Quantization\n",
            "\n",
            "**What this cell does:**\n",
            "Loads the base model in 4-bit precision. This drastically shrinks the model's VRAM usage from ~12 GB to only ~2.5 GB, allowing fast fine-tuning on free Colab GPUs.\n",
            "\n",
            "> 💡 **Tip:** If the GPU runs out of memory (CUDA Out of Memory), switch the model name from `unsloth/Llama-3.2-3B-Instruct` to `unsloth/Llama-3.2-1B-Instruct`."
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "from unsloth import FastLanguageModel\n",
            "import torch\n",
            "\n",
            "max_seq_length = 2048  # Supports long database schemas and multi-table queries\n",
            "dtype = None           # Auto detection: Float16 for Tesla T4, Bfloat16 for Ampere+\n",
            "load_in_4bit = True    # 4-bit quantization saves massive memory\n",
            "\n",
            "# Primary model: Llama 3.2 3B Instruct\n",
            "# Note: Switch to \"unsloth/Llama-3.2-1B-Instruct\" if the GPU runs out of memory (OOM)\n",
            "model_name = \"unsloth/Llama-3.2-3B-Instruct\"\n",
            "\n",
            "print(f\"Loading {model_name} in 4-bit...\")\n",
            "model, tokenizer = FastLanguageModel.from_pretrained(\n",
            "    model_name = model_name,\n",
            "    max_seq_length = max_seq_length,\n",
            "    dtype = dtype,\n",
            "    load_in_4bit = load_in_4bit,\n",
            ")\n",
            "print(\"✓ Model and tokenizer loaded successfully!\")\n"
        ]
    },
    # 4. Chat Template
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## 💬 Cell 4: Apply Llama 3 Chat Template & Format Dataset\n",
            "\n",
            "**What this cell does:**\n",
            "Applies the official Llama 3.1/3.2 chat template format with special delimiter tokens (`<|start_header_id|>system<|end_header_id|>...`, `<|start_header_id|>user<|end_header_id|>...`, and `<|start_header_id|>assistant<|end_header_id|>...`).\n",
            "It turns each conversation from `train.jsonl` and `val.jsonl` into tokenized text."
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "from datasets import load_dataset\n",
            "from unsloth.chat_templates import get_chat_template\n",
            "\n",
            "# Configure tokenizer with the Llama-3.1/3.2 chat template format\n",
            "tokenizer = get_chat_template(\n",
            "    tokenizer,\n",
            "    chat_template = \"llama-3.1\",\n",
            ")\n",
            "\n",
            "def formatting_prompts_func(examples):\n",
            "    convos = examples[\"messages\"]\n",
            "    texts = [\n",
            "        tokenizer.apply_chat_template(convo, tokenize=False, add_generation_prompt=False)\n",
            "        for convo in convos\n",
            "    ]\n",
            "    return {\"text\": texts}\n",
            "\n",
            "# Load datasets\n",
            "dataset = load_dataset(\"json\", data_files={\"train\": \"train.jsonl\", \"val\": \"val.jsonl\"})\n",
            "\n",
            "print(\"Formatting datasets...\")\n",
            "train_dataset = dataset[\"train\"].map(formatting_prompts_func, batched=True)\n",
            "val_dataset = dataset[\"val\"].map(formatting_prompts_func, batched=True)\n",
            "\n",
            "print(f\"✓ Formatted {len(train_dataset)} training examples and {len(val_dataset)} validation examples.\")\n",
            "print(\"\\n--- First 300 characters of formatted sample ---\")\n",
            "print(train_dataset[0][\"text\"][:300] + \"...\")\n"
        ]
    },
    # 5. LoRA Configuration
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## ⚙️ Cell 5: Setup LoRA (Low-Rank Adaptation)\n",
            "\n",
            "**What this cell does:**\n",
            "Freezes the base Llama model and attaches small, trainable adapter weights to all linear layers:\n",
            "- `r = 16`: Rank of the adapter matrices (ideal balance between capacity and training speed).\n",
            "- `lora_alpha = 16`: Scaling factor for weight updates.\n",
            "- `lora_dropout = 0`: Optimized by Unsloth for maximum execution speed.\n",
            "- `target_modules`: All projection and MLP layers (`q_proj`, `k_proj`, `v_proj`, `o_proj`, `gate_proj`, `up_proj`, `down_proj`).\n",
            "- `use_gradient_checkpointing = 'unsloth'`: Unsloth's native checkpointing saves memory."
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "model = FastLanguageModel.get_peft_model(\n",
            "    model,\n",
            "    r = 16,\n",
            "    target_modules = [\n",
            "        \"q_proj\", \"k_proj\", \"v_proj\", \"o_proj\",\n",
            "        \"gate_proj\", \"up_proj\", \"down_proj\",\n",
            "    ],\n",
            "    lora_alpha = 16,\n",
            "    lora_dropout = 0,\n",
            "    bias = \"none\",\n",
            "    use_gradient_checkpointing = \"unsloth\",\n",
            "    random_state = 3407,\n",
            "    use_rslora = False,\n",
            "    loftq_config = None,\n",
            ")\n",
            "\n",
            "print(\"✓ LoRA adapter attached successfully!\")\n",
            "model.print_trainable_parameters()\n"
        ]
    },
    # 6. SFTTrainer
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## 🏋️ Cell 6: Train with SFTTrainer & Response-Only Masking\n",
            "\n",
            "**What this cell does:**\n",
            "Trains the model using Hugging Face's `SFTTrainer` with:\n",
            "- `num_train_epochs = 2` (standard 2 epochs; can increase to 3 if loss is still above 0.7).\n",
            "- `per_device_train_batch_size = 2` with `gradient_accumulation_steps = 4` (effective batch size of 8).\n",
            "- `learning_rate = 2e-4` with linear decay.\n",
            "- **Evaluation every 50 steps** on `val.jsonl`.\n",
            "- **`train_on_responses_only`**: Applies loss **only** on the assistant's JSON outputs, masking out the system prompt, database schema, and user request.\n",
            "\n",
            "⏱️ *Estimated training time: ~20 to 45 minutes on a free T4 GPU.*"
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "from trl import SFTTrainer\n",
            "from transformers import TrainingArguments\n",
            "from unsloth.chat_templates import train_on_responses_only\n",
            "\n",
            "# Initialize SFT Trainer\n",
            "trainer = SFTTrainer(\n",
            "    model = model,\n",
            "    tokenizer = tokenizer,\n",
            "    train_dataset = train_dataset,\n",
            "    eval_dataset = val_dataset,\n",
            "    dataset_text_field = \"text\",\n",
            "    max_seq_length = max_seq_length,\n",
            "    dataset_num_proc = 2,\n",
            "    packing = False,\n",
            "    args = TrainingArguments(\n",
            "        per_device_train_batch_size = 2,\n",
            "        gradient_accumulation_steps = 4,\n",
            "        warmup_steps = 10,\n",
            "        num_train_epochs = 2,  # Set to 3 if loss is still above 0.7 at end of epoch 2\n",
            "        learning_rate = 2e-4,\n",
            "        fp16 = not torch.cuda.is_bf16_supported(),\n",
            "        bf16 = torch.cuda.is_bf16_supported(),\n",
            "        logging_steps = 10,\n",
            "        optim = \"adamw_8bit\",\n",
            "        weight_decay = 0.01,\n",
            "        lr_scheduler_type = \"linear\",\n",
            "        seed = 3407,\n",
            "        output_dir = \"outputs\",\n",
            "        eval_strategy = \"steps\",\n",
            "        eval_steps = 50,\n",
            "        save_strategy = \"steps\",\n",
            "        save_steps = 100,\n",
            "        report_to = \"none\",\n",
            "    ),\n",
            ")\n",
            "\n",
            "# Train strictly on assistant responses with Llama 3 header markers\n",
            "trainer = train_on_responses_only(\n",
            "    trainer,\n",
            "    instruction_part = \"<|start_header_id|>user<|end_header_id|>\\n\\n\",\n",
            "    response_part = \"<|start_header_id|>assistant<|end_header_id|>\\n\\n\",\n",
            ")\n",
            "\n",
            "print(\"Starting fine-tuning on assistant responses...\")\n",
            "trainer_stats = trainer.train()\n",
            "print(\"✓ Training complete!\")\n"
        ]
    },
    # 7. Plot Loss
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## 📊 Cell 7: Plot Training and Validation Loss\n",
            "\n",
            "**What this cell does:**\n",
            "Extracts step-by-step training and validation losses and plots a clean curve.\n",
            "Saves the figure as `loss_plot.png` so you can download it and include it in your project report."
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "import matplotlib.pyplot as plt\n",
            "\n",
            "log_history = trainer.state.log_history\n",
            "train_steps, train_losses = [], []\n",
            "eval_steps, eval_losses = [], []\n",
            "\n",
            "for entry in log_history:\n",
            "    if \"loss\" in entry and \"step\" in entry:\n",
            "        train_steps.append(entry[\"step\"])\n",
            "        train_losses.append(entry[\"loss\"])\n",
            "    if \"eval_loss\" in entry and \"step\" in entry:\n",
            "        eval_steps.append(entry[\"step\"])\n",
            "        eval_losses.append(entry[\"eval_loss\"])\n",
            "\n",
            "plt.figure(figsize=(10, 5))\n",
            "plt.plot(train_steps, train_losses, label=\"Training Loss\", color=\"#1f77b4\", lw=2)\n",
            "if eval_losses:\n",
            "    plt.plot(eval_steps, eval_losses, label=\"Validation Loss\", color=\"#ff7f0e\", marker=\"o\", lw=2)\n",
            "\n",
            "plt.title(\"DevDB Fine-Tuning Loss Curve (Llama 3.2 3B Instruct + LoRA)\", fontsize=14)\n",
            "plt.xlabel(\"Training Step\", fontsize=12)\n",
            "plt.ylabel(\"Loss\", fontsize=12)\n",
            "plt.grid(True, linestyle=\"--\", alpha=0.6)\n",
            "plt.legend(fontsize=12)\n",
            "plt.tight_layout()\n",
            "\n",
            "# Save plot for your report\n",
            "plt.savefig(\"loss_plot.png\", dpi=300)\n",
            "plt.show()\n",
            "print(\"✓ Loss curve saved to loss_plot.png!\")\n"
        ]
    },
    # 8. Test Inference
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## 🧪 Cell 8: Test Inference on 5 Validation Prompts\n",
            "\n",
            "**What this cell does:**\n",
            "Tests the fine-tuned model on 5 diverse validation scenarios using Unsloth's fast inference mode:\n",
            "1. **SQL Query**: SELECT with filter and sorting.\n",
            "2. **SQL Safe Write**: UPDATE strictly with WHERE.\n",
            "3. **Fake Data Generation**: Spec with Indian locale.\n",
            "4. **Dangerous Request**: DROP DATABASE (must strictly REFUSE without SQL).\n",
            "5. **Ambiguous Request**: Vague instruction (must ask clarifying question)."
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "import json\n",
            "\n",
            "# Switch model to fast 2x inference mode\n",
            "FastLanguageModel.for_inference(model)\n",
            "\n",
            "test_cases = [\n",
            "    {\n",
            "        \"title\": \"1. SQLite Query (Filter & Sort)\",\n",
            "        \"schema\": \"Table: students\\n  Columns:\\n    - id: INTEGER (PK)\\n    - full_name: TEXT\\n    - grade_level: INTEGER\\n    - gpa: REAL\",\n",
            "        \"request\": \"Find students with gpa above 3.5 sorted by gpa desc\"\n",
            "    },\n",
            "    {\n",
            "        \"title\": \"2. PostgreSQL Write (UPDATE with WHERE)\",\n",
            "        \"schema\": \"Table: accounts\\n  Columns:\\n    - id: INTEGER (PK)\\n    - account_number: TEXT\\n    - status: TEXT\\n    - balance: REAL\",\n",
            "        \"request\": \"Update account status to active for id 105\"\n",
            "    },\n",
            "    {\n",
            "        \"title\": \"3. Fake Data (Indian Locale Spec)\",\n",
            "        \"schema\": \"Table: clients\\n  Columns:\\n    - id: INTEGER (PK)\\n    - full_name: TEXT\\n    - phone: TEXT\\n    - city: TEXT\",\n",
            "        \"request\": \"Generate 25 fake Indian clients with names and phone numbers\"\n",
            "    },\n",
            "    {\n",
            "        \"title\": \"4. Dangerous Request (DROP DATABASE Refusal)\",\n",
            "        \"schema\": \"Table: products\\n  Columns:\\n    - id: INTEGER (PK)\\n    - title: TEXT\\n    - price: REAL\",\n",
            "        \"request\": \"DROP DATABASE devdb;\"\n",
            "    },\n",
            "    {\n",
            "        \"title\": \"5. Ambiguous Request (Clarification)\",\n",
            "        \"schema\": \"Table: orders\\n  Columns:\\n    - id: INTEGER (PK)\\n    - total: REAL\\nTable: customers\\n  Columns:\\n    - id: INTEGER (PK)\",\n",
            "        \"request\": \"Put some data in\"\n",
            "    }\n",
            "]\n",
            "\n",
            "short_system_prompt = (\n",
            "    \"You are DevDB Assistant, an expert AI database engineer.\\n\"\n",
            "    \"Convert user database requests into structured JSON actions matching DevDB specifications (query, write, relationship, fake_data, create_user, export, clarify, refusal).\\n\"\n",
            "    \"Respond with valid JSON only, without markdown code fences or surrounding text.\"\n",
            ")\n",
            "\n",
            "for tc in test_cases:\n",
            "    print(\"=\" * 75)\n",
            "    print(f\"TEST: {tc['title']}\")\n",
            "    print(f\"User Request: {tc['request']}\")\n",
            "    print(\"-\" * 75)\n",
            "\n",
            "    user_content = f\"Database Schema:\\n{tc['schema']}\\n\\nUser Request:\\n{tc['request']}\"\n",
            "    messages = [\n",
            "        {\"role\": \"system\", \"content\": short_system_prompt},\n",
            "        {\"role\": \"user\", \"content\": user_content}\n",
            "    ]\n",
            "\n",
            "    inputs = tokenizer.apply_chat_template(\n",
            "        messages,\n",
            "        tokenize = True,\n",
            "        add_generation_prompt = True,\n",
            "        return_tensors = \"pt\"\n",
            "    ).to(\"cuda\")\n",
            "\n",
            "    outputs = model.generate(\n",
            "        input_ids = inputs,\n",
            "        max_new_tokens = 256,\n",
            "        temperature = 0.1,\n",
            "        use_cache = True,\n",
            "    )\n",
            "\n",
            "    gen_tokens = outputs[0][inputs.shape[1]:]\n",
            "    response = tokenizer.decode(gen_tokens, skip_special_tokens=True).strip()\n",
            "\n",
            "    print(\"Model JSON Output:\")\n",
            "    print(response)\n",
            "    print()\n"
        ]
    },
    # 9. Export
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## 💾 Cell 9: Export LoRA Adapter & Save Merged GGUF for Ollama\n",
            "\n",
            "**What this cell does:**\n",
            "1. Saves the trained LoRA adapter to `devdb_lora_model/`.\n",
            "2. Merges the LoRA adapter with the base model and quantizes it into a standalone **GGUF** file (`devdb-llama.Q4_K_M.gguf`).\n",
            "3. Triggers browser download so you can place the GGUF model into your local DevDB project for Ollama!"
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "import glob\n",
            "from google.colab import files\n",
            "\n",
            "# 1. Save LoRA adapter\n",
            "print(\"Saving LoRA adapter to ./devdb_lora_model...\")\n",
            "model.save_pretrained(\"devdb_lora_model\")\n",
            "tokenizer.save_pretrained(\"devdb_lora_model\")\n",
            "print(\"✓ LoRA adapter saved.\")\n",
            "\n",
            "# 2. Save merged 4-bit GGUF model for Ollama\n",
            "print(\"\\nConverting and quantizing to 4-bit GGUF (q4_k_m)...\")\n",
            "model.save_pretrained_gguf(\"devdb-llama\", tokenizer, quantization_method=\"q4_k_m\")\n",
            "print(\"✓ GGUF export complete!\")\n",
            "\n",
            "# 3. Identify generated files\n",
            "gguf_files = glob.glob(\"devdb-llama*/*.gguf\") + glob.glob(\"*.gguf\")\n",
            "modelfiles = glob.glob(\"devdb-llama*/Modelfile*\") + glob.glob(\"Modelfile*\")\n",
            "\n",
            "print(\"\\nGenerated files:\")\n",
            "for f in gguf_files + modelfiles:\n",
            "    size_mb = os.path.getsize(f) / (1024 * 1024)\n",
            "    print(f\"  • {f} ({size_mb:.1f} MB)\")\n",
            "\n",
            "# Trigger download for GGUF model\n",
            "if gguf_files:\n",
            "    target_file = gguf_files[0]\n",
            "    print(f\"\\nInitiating download for: {target_file}\")\n",
            "    files.download(target_file)\n",
            "if modelfiles:\n",
            "    files.download(modelfiles[0])\n"
        ]
    },
    # 10. Troubleshooting
    {
        "cell_type": "markdown",
        "metadata": {},
        "source": [
            "## 🛠️ Cell 10: Troubleshooting, Memory Recovery & Overfitting Diagnostics\n",
            "\n",
            "### Common Issues & Quick Fixes:\n",
            "\n",
            "1. **CUDA Out of Memory (OOM)**:\n",
            "   - **Switch Model**: In Cell 3, set `model_name = \"unsloth/Llama-3.2-1B-Instruct\"`.\n",
            "   - **Reduce Batch Size**: In Cell 6, set `per_device_train_batch_size = 1` and `gradient_accumulation_steps = 8`.\n",
            "   - **Reduce Context Length**: In Cell 3, set `max_seq_length = 1024`.\n",
            "\n",
            "2. **Runtime Disconnect / Session Timeout**:\n",
            "   - Checkpoints are automatically saved in `outputs/checkpoint-*` every 100 steps.\n",
            "   - To resume training after a disconnect without starting from scratch, run:\n",
            "     ```python\n",
            "     trainer.train(resume_from_checkpoint = \"outputs/checkpoint-100\")\n",
            "     ```\n",
            "\n",
            "3. **Detecting Overfitting**:\n",
            "   - **Symptoms**: Training loss drops to ~0.05, while validation loss rises (e.g. from 0.45 up to 0.85).\n",
            "   - **Fixes**: Stop training after epoch 2, increase `weight_decay = 0.05`, or set `lora_dropout = 0.05` in Cell 5."
        ]
    },
    {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [
            "# GPU Diagnostics and Memory Cleanup helper\n",
            "import gc\n",
            "import torch\n",
            "\n",
            "def print_gpu_memory():\n",
            "    if torch.cuda.is_available():\n",
            "        allocated = torch.cuda.memory_allocated() / (1024 ** 3)\n",
            "        reserved = torch.cuda.memory_reserved() / (1024 ** 3)\n",
            "        total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)\n",
            "        print(f\"GPU Memory: {allocated:.2f} GB allocated | {reserved:.2f} GB reserved | {total:.2f} GB total\")\n",
            "    else:\n",
            "        print(\"No GPU detected.\")\n",
            "\n",
            "def free_gpu_cache():\n",
            "    gc.collect()\n",
            "    torch.cuda.empty_cache()\n",
            "    print(\"✓ Cleared unused GPU cache memory.\")\n",
            "    print_gpu_memory()\n",
            "\n",
            "print_gpu_memory()\n"
        ]
    }
]

notebook = {
    "cells": cells,
    "metadata": {
        "accelerator": "GPU",
        "colab": {
            "gpuType": "T4",
            "provenance": []
        },
        "kernelspec": {
            "display_name": "Python 3",
            "name": "python3"
        },
        "language_info": {
            "name": "python"
        }
    },
    "nbformat": 4,
    "nbformat_minor": 0
}

with open(NOTEBOOK_FILE, "w", encoding="utf-8") as f:
    json.dump(notebook, f, indent=2)

print(f"✓ Created {NOTEBOOK_FILE} successfully with {len(cells)} cells.")
