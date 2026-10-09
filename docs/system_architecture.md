# System Architecture

```text
data/raw/diabetic_data.csv
        |
        v
src.preprocessing
  - missing value handling
  - hospice/death filtering
  - ICD-9 category mapping
  - GroupShuffleSplit by patient_nbr
        |
        v
src.train + src.evaluate
  - Logistic Regression
  - Random Forest
  - XGBoost
  - MLflow local tracking
        |
        v
models/
  - best_model.joblib
  - preprocessing_pipeline.joblib
  - model_metadata.json
        |
        v
api/
  - /health
  - /model-info
  - /predict
  - /explain
  - /feedback
        |
        v
dashboard/app.py
  - Explainability tab -> dashboard/components/shap_explorer.py
        |  JSON props (stored prediction + SHAP top_features, unchanged)
        v
  frontend/dist  (React + TypeScript explorer, Streamlit custom-component iframe)
```

The optional LLM path uses LangChain and LangSmith only when keys are configured. Offline demo mode uses rule-based explanations.

## Frontend component

The Risk & SHAP Explainability Explorer is a React + TypeScript Streamlit custom
component (`streamlit.components.v1.declare_component`). Its source lives in
`frontend/` and is compiled by Vite into static files in `frontend/dist`, which
Streamlit serves directly. Node.js and npm are frontend build tooling only
(Vite dev server, `tsc` type checking, Vitest, production build); there is no
Node.js backend or extra runtime service.

- **Data flow:** `render_feature_contributions` passes `st.session_state.last_result`
  to `build_explorer_props`, which reshapes (but never changes) the probability,
  backend risk label, threshold, SHAP contributions, and disclaimer into JSON.
  Streamlit posts it to the iframe as a `streamlit:render` message.
- **No new logic:** the component does not run inference, recompute SHAP values,
  or call the API. Sorting, filtering, and inspection happen in the browser and
  are not sent back to Python.
- **Fallback:** if `frontend/dist` is missing or
  `CLINICAL_TRACE_DISABLE_REACT_EXPLORER=1`, the original static Streamlit bars
  render instead.
- **Deployment:** the Hugging Face Space Dockerfile builds the frontend in a
  `node` stage and copies only `frontend/dist` into the `python:3.12-slim`
  runtime image, which still runs FastAPI (internal 8000) and Streamlit
  (public 7860).
