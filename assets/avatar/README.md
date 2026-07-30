# Custom photo avatar — source asset

This folder holds the **source photo** used to train the custom photo avatar
that the app talks to via Voice Live.

The trained avatar itself lives in the Microsoft Foundry /Azure Speech / AI Services resource
pointed to by `AZURE_VOICELIVE_ENDPOINT` — this image is kept here only for
reproducibility (re-training, swapping characters, audit trail).

## Fill in when you commit a photo

- **File:** `Nuru.jpg`
- **Trained character name:** `Nuru`
- **Foundry resource:** `eastus2`
- **Trained on:** `2026-05-15`
- **Source / consent:** `Generated image, not a real person`

## Notes

The photo is kept for reproducibility only. Retraining or swapping the character is
done in the Foundry / Speech resource, not here — replacing this file does **not**
change the avatar the app talks to.