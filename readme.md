# Export an IndoPak Nastaleeq Quran PDF (15 Lines per Page)

Follow the instructions below to export an **IndoPak Nastaleeq Quran PDF** with **15 lines per page** and **20 pages per Juz**.

### 1. Download and Extract
Download the `.zip` file and extract its contents to your preferred directory.

### 2. Create a Python Virtual Environment
Open a terminal in the extracted project directory and run:

```powershell
python -m venv venv
```

### 3. Activate the Virtual Environment
Activate the virtual environment using the following PowerShell command:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned; .\venv\Scripts\Activate.ps1
```

**Note:** If you are using a recent version of Visual Studio Code, the virtual environment may activate automatically when you open a new terminal in the project directory.

### 4. Install the Required Dependencies
Run the following command to install all the required Python packages:

```bash
pip install -r requirements.txt
```

### 5. Export the Quran PDF
Finally, run the following command to generate the Quran PDF:

```bash
python export_mushaf_pdf.py
```