# 🚀 DevDB Assistant: Google Colab Fine-Tuning Guide (Phase 12C)

This guide provides step-by-step instructions for fine-tuning **Llama 3.2 3B Instruct** on your custom **DevDB database assistant dataset** using **Unsloth** and **LoRA** on a **free Google Colab T4 GPU**.

---

## ⏱️ Quick Summary & Hardware Requirements

| Parameter | Specification | Notes |
| :--- | :--- | :--- |
| **Model** | `unsloth/Llama-3.2-3B-Instruct` | 4-bit quantized base model (~2.5 GB VRAM) |
| **Fallback Model** | `unsloth/Llama-3.2-1B-Instruct` | If GPU memory is heavily constrained |
| **Hardware** | **Free Google Colab T4 GPU** | Standard 15 GB VRAM (No paid Colab Pro required) |
| **Dataset Size** | **1,051 validated examples** | `train.jsonl` (893) + `val.jsonl` (158) |
| **Estimated Training Time** | **20 to 45 minutes** | ~25 minutes on average for 2 epochs |
| **Output Artifacts** | `devdb-llama.Q4_K_M.gguf`, `loss_plot.png` | Standalone GGUF file for local Ollama execution |

---

## 📁 Prerequisites: Files You Need

Ensure you have the following files from your local workspace:
1. `finetune/train_colab.ipynb` (The complete Colab notebook)
2. `finetune/dataset/train.jsonl` (893 training examples)
3. `finetune/dataset/val.jsonl` (158 validation examples)

---

## 🛠️ Step-by-Step Colab Walkthrough

### Step 1: Open Google Colab and Upload the Notebook
1. Navigate in your browser to [colab.research.google.com](https://colab.research.google.com).
2. In the modal that appears, click the **Upload** tab.
3. Browse and select `finetune/train_colab.ipynb` from your computer.

---

### Step 2: Switch to Free T4 GPU Runtime
1. In the top menu bar, click **Runtime** ➔ **Change runtime type**.
2. Under **Hardware accelerator**, select **T4 GPU**.
3. Ensure **GPU type** is set to `T4` and click **Save**.
4. In the top right corner, click **Connect**. Once connected, you should see `RAM` and `Disk` with a green checkmark indicating GPU allocation.

---

### Step 3: Run Cell 1 — Install Dependencies
- Click the **Play** button on **Cell 1**.
- This cell installs `unsloth`, `trl`, `peft`, `accelerate`, `bitsandbytes`, and `datasets`.
- *Time: ~1 to 2 minutes.*

---

### Step 4: Run Cell 2 — Upload Datasets
- Click the **Play** button on **Cell 2**.
- Click **Choose Files** and upload both `train.jsonl` and `val.jsonl`.
- The cell verifies that both files exist and reports the exact record counts.

---

### Step 5: Run Cells 3 to 5 — Model Setup & LoRA Configuration
- **Cell 3 (Load Model)**: Downloads and initializes `unsloth/Llama-3.2-3B-Instruct` in 4-bit precision.
  > 💡 *Note: If Colab ever warns of GPU memory constraints, switch `model_name = "unsloth/Llama-3.2-1B-Instruct"` in Cell 3.*
- **Cell 4 (Chat Template)**: Applies the Llama 3 delimiter format (`<|start_header_id|>...`) to format `train.jsonl` and `val.jsonl`.
- **Cell 5 (LoRA Adapter)**: Configures LoRA with:
  - Rank $r = 16$, $\alpha = 16$, dropout = $0$.
  - All linear projection modules: `q_proj`, `k_proj`, `v_proj`, `o_proj`, `gate_proj`, `up_proj`, `down_proj`.
  - Unsloth gradient checkpointing enabled.

---

### Step 6: Run Cell 6 — SFT Training with Response Masking
- Click **Play** on **Cell 6** to start fine-tuning.
- **Why `train_on_responses_only` matters**:
  The loss is computed **only** on the assistant's JSON response. The system prompt, database schema, and user request are masked out so the model does not waste capacity memorizing schemas.
- **Training Parameters**:
  - `num_train_epochs = 2`
  - `per_device_train_batch_size = 2`
  - `gradient_accumulation_steps = 4` (effective batch size = 8)
  - `learning_rate = 2e-4`
  - `eval_steps = 50` (evaluates against `val.jsonl` periodically)
- *Expected Time: ~20 to 35 minutes on T4 GPU.*
- Watch the training progress bar and step loss output.

---

### Step 7: Run Cell 7 & 8 — Loss Curve & Verification Tests
- **Cell 7**: Generates a step-by-step loss plot comparing training loss and validation loss. Saves `loss_plot.png`.
- **Cell 8**: Runs 5 verification prompts using Unsloth's fast 2x inference engine:
  1. *SQLite Query (Filter & Sort)*: Verifies clean SQL syntax and safety limits.
  2. *PostgreSQL Write (UPDATE with WHERE)*: Verifies proper clause constraints.
  3. *Fake Data (Indian Locale)*: Verifies synthesis spec with `locale: en_IN`.
  4. *Dangerous Request (DROP DATABASE)*: Verifies strict refusal with empty SQL.
  5. *Ambiguous Request*: Verifies clarification response.

---

### Step 8: Run Cell 9 — Export & Download GGUF
- Saves the raw LoRA adapter to `devdb_lora_model/`.
- Merges adapter weights into the base model and quantizes to 4-bit `q4_k_m` format.
- Automatically triggers a browser download for the file:
  `devdb-llama.Q4_K_M.gguf` (~2.0 GB).

---

## 💻 Local Setup: Loading the Model into Ollama

Once `devdb-llama.Q4_K_M.gguf` has downloaded to your local machine, follow these steps to use it with DevDB:

### 1. Place the File
Move the downloaded `.gguf` file into your local project directory or a convenient location, e.g.:
```bash
devdb-llama.Q4_K_M.gguf
```

### 2. Create an Ollama `Modelfile`
In the same folder, create a file named `Modelfile` with the following content:
```dockerfile
FROM ./devdb-llama.Q4_K_M.gguf

TEMPLATE """<|start_header_id|>system<|end_header_id|>
{{ .System }}<|eot_id|><|start_header_id|>user<|end_header_id|>
{{ .Prompt }}<|eot_id|><|start_header_id|>assistant<|end_header_id|>
{{ .Response }}<|eot_id|>"""

PARAMETER stop "<|start_header_id|>"
PARAMETER stop "<|end_header_id|>"
PARAMETER stop "<|eot_id|>"
PARAMETER temperature 0.1
PARAMETER num_ctx 4096
```

### 3. Register the Model with Ollama
Open your local terminal and execute:
```bash
ollama create devdb-llama -f Modelfile
```
Verify the model is listed:
```bash
ollama list
```

### 4. Configure DevDB to Use Your Fine-Tuned Model
Update your project `.env` file:
```env
LLM_PROVIDER=ollama
LLM_MODEL=devdb-llama
OLLAMA_URL=http://localhost:11434
```

### 5. Launch DevDB CLI
```bash
python -m devdb.cli
```
Ask:
```text
DevDB> which model am I using?
🤖 DevDB is currently using the 'ollama' provider with model 'devdb-llama' via http://localhost:11434.
```

---

## 🔍 Troubleshooting & Common Questions

### 1. "CUDA Out of Memory" (OOM) during training
- **Solution A**: In Cell 3, change `model_name = "unsloth/Llama-3.2-1B-Instruct"`. The 1B model uses half the VRAM and trains in under 15 minutes.
- **Solution B**: In Cell 6, change `per_device_train_batch_size = 1` and `gradient_accumulation_steps = 8`.
- **Solution C**: In Cell 3, change `max_seq_length = 1024`.

### 2. Google Colab Disconnected or Timed Out
- Google Colab free tier sessions can disconnect if your browser tab is closed. Keep the tab open while Cell 6 is running.
- Checkpoints are saved in `outputs/checkpoint-*` every 100 steps.
- To resume without starting over, modify Cell 6:
  ```python
  trainer.train(resume_from_checkpoint = "outputs/checkpoint-100")
  ```

### 3. How to check for Overfitting?
- Look at the plot generated in Cell 7 (`loss_plot.png`).
- **Healthy training**: Both training loss and validation loss decrease smoothly (typically starting around ~1.8 and stabilizing around ~0.35 - 0.55).
- **Overfitting**: Training loss drops to near zero (< 0.1) while validation loss begins curving upward. If this occurs, 2 epochs is already sufficient—do not run epoch 3.
