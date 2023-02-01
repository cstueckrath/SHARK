# Sharded Bloom 560m Demo

First, set up the environment normally:

```
./setup_venv.sh
source shark.venv/bin/activate
```

Next, make sure you have the correct version of transformers installed:

```
pip uninstall transformers
pip install transformers==4.21.2
```

Next, make the directory where you want to store the model:
E.G:
```
mkdir 560m
```

The first time you run the demo, run:
```
python shark/examples/shark_inference/sharded_bloom/sharded_bloom.py --model_path 560m --recompile True --download True
```
The first arg specifies the model directory, the second arg specifies you want to recompile, and the third specifies you want to download the model.
Afterwards, the model can be run:

```
python shark/examples/shark_inference/sharded_bloom/sharded_bloom.py --model_path 560m
```

during the runtime you will be promted to provide the prompt and the number of tokens you want to generate

