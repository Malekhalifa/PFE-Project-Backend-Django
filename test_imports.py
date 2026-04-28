import os, sys, importlib
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

base_dirs = ['agents', 'services', 'tasks']
failures = []

for base_dir in base_dirs:
    for root, _, files in os.walk(base_dir):
        for f in files:
            if f.endswith('.py') and not f.startswith('__'):
                mod_name = os.path.relpath(os.path.join(root, f), '.').replace('.py', '').replace('/', '.')
                try:
                    importlib.import_module(mod_name)
                    print(f"OK: {mod_name}")
                except Exception as e:
                    print(f"FAIL: {mod_name} -> {repr(e)}")
                    failures.append((mod_name, e))

if failures:
    sys.exit(1)
else:
    print("ALL CLEAR")
