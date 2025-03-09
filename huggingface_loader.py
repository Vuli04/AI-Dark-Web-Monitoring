from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline
import os

# Choose your model
MODEL_NAME = "distilbert-base-uncased-finetuned-sst-2-english"
MODEL_PATH = f"./models/{MODEL_NAME}"  # Local directory to store the model

# Function to download and load the model
def load_model(model_name, model_path):
    # Check if model is already downloaded
    if not os.path.exists(model_path):
        print(f"📥 Downloading model: {model_name} ...")
        model = AutoModelForSequenceClassification.from_pretrained(model_name)
        tokenizer = AutoTokenizer.from_pretrained(model_name)

        # Save the model locally
        os.makedirs(model_path, exist_ok=True)
        model.save_pretrained(model_path)
        tokenizer.save_pretrained(model_path)
        print(f"✅ Model saved at {model_path}")
    else:
        print(f"🔍 Model already exists at {model_path}")

    # Load the model and tokenizer
    model = AutoModelForSequenceClassification.from_pretrained(model_path)
    tokenizer = AutoTokenizer.from_pretrained(model_path)

    return model, tokenizer

# Load model (download if not already)
model, tokenizer = load_model(MODEL_NAME, MODEL_PATH)

# Create a classification pipeline
classifier = pipeline("text-classification", model=model, tokenizer=tokenizer)

# Test the model
text = "The dark web contains a lot of hidden marketplaces."
result = classifier(text)

print("🔍 AI Analysis Result:", result)
