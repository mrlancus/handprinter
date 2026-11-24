# Training on your handwriting: step by step

Experiments → **Ready to train?** shows each step below with a tick, and highlights the next one. Nothing starts on its own.

## 1. Approve words (Check)

- At least 50 approved words are needed. About 1,000 or more give a much closer likeness. More pages of ordinary notes help more than more sheets: copied sheet text tends to be neater than your natural writing.
- DiffusionPen only knows letters, digits and `! " # & ' ( ) * + , - . / : ; ?`.
  - Words with other symbols (maths, £, é) stay in your dataset but are not trained on.
  - The checklist says how many of your words it can use.

## 2. Export a snapshot (Dataset)

- The snapshot freezes your approved words and splits them into three sets:
  - **training:** the model learns from these;
  - **validation and test:** never shown to the model, so you can see how it does on words it has not seen.
- **Tick iPad** if you write in GoodNotes. The form now starts with the kinds of page you actually have. The checklist warns if a snapshot leaves your iPad pages out.
- Export a new snapshot whenever you have approved many more words. The checklist says when the latest one is behind.

## 3. Set up DiffusionPen on the PC (once)

1. Run `SetupModels.bat`. It creates a CUDA Python environment and gets the DiffusionPen code.
2. Restart Handprinter.
3. In Experiments, press **Download weights** (about 1.5 GB).
4. Check that the **cuda** line in DiffusionPen's card is ticked. If it is not, training would run on the CPU, which is far too slow. Update the NVIDIA driver and run `SetupModels.bat` again.
5. Press **Run memory preflight**. It checks that the model fits in the graphics card's memory.

## 4. Baseline (recommended)

1. Create a **style profile** from the snapshot. It picks 5 of your crops as style examples.
2. Press **Run baseline**. This is what the untrained model makes from your crops alone. Keep it, to judge what training adds.

## 5. Fine-tune

- The **Steps** box starts at a suggestion of about 25 passes over your training words (500 to 6,000 steps). That is a starting point, not a tuned value.
- If results copy whole training words too closely, or look smeared, try fewer steps.
- If letters still look like someone else's writing, try more steps, or approve more words.
- The time on your graphics card has not been measured yet. Expect tens of minutes for a few thousand steps on an RTX 2060-class card. Progress shows in Background jobs.
- When it finishes, choose it under **Weights** in Generate. Then compare it blind against the baseline (Blind comparison).

## What it does not do yet

- **Handwritten maths:** neither model can write maths symbols. Studio typesets maths.
- **Eruku:** it copies style from examples only. Its authors publish no way to fine-tune it on one writer.
