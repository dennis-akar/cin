#!/bin/bash

python -m exp.run_mol_exp \
  --device 0 \
  --start_seed 0 \
  --stop_seed 0 \
  --exp_name cwn-qm9-test-small \
  --dataset QM9 \
  --train_eval_period 1 \
  --epochs 100 \
  --batch_size 128 \
  --drop_rate 0.0 \
  --drop_position lin2 \
  --emb_dim 48 \
  --max_dim 2 \
  --final_readout sum \
  --init_method sum \
  --lr 0.001 \
  --graph_norm bn \
  --model qm9_embed_sparse_cin \
  --nonlinearity relu \
  --num_layers 2 \
  --readout sum \
  --max_ring_size 18 \
  --task_type regression \
  --eval_metric mae \
  --minimize \
  --lr_scheduler 'ReduceLROnPlateau' \
  --use_coboundaries True \
  --use_edge_features \
  --early_stop \
  --lr_scheduler_patience 20 \
  --dump_curves \
  --preproc_jobs 32
