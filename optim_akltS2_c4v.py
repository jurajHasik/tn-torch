import torch
import argparse
import config as cfg
from ipeps import *
from c4v import *
from ctm.one_site_c4v.env_c4v import *
from ctm.one_site_c4v import ctmrg_c4v
from models import akltS2
from optim.ad_optim import optimize_state

if __name__=='__main__':
    # parse command line args and build necessary configuration objects
    parser= cfg.get_args_parser()
    # additional model-dependent arguments
    args = parser.parse_args()
    cfg.configure(args)
    cfg.print_config()
    torch.set_num_threads(args.omp_cores)

    model = akltS2.AKLTS2_C4V_BIPARTITE()
    
    # initialize an ipeps
    if args.instate!=None:
        state = read_ipeps(args.instate, vertexToSite=None)
        assert len(state.sites)==1, "Not a 1-site ipeps"
        if args.bond_dim > max(state.get_aux_bond_dims()):
            # extend the auxiliary dimensions
            state = extend_bond_dim(state, args.bond_dim)
        add_random_noise(state, args.instate_noise)
        state.sites[(0,0)]= make_c4v_symm(state.sites[(0,0)])
        state.sites[(0,0)]= state.sites[(0,0)]/torch.max(torch.abs(state.sites[(0,0)]))
    elif args.ipeps_init_type=='RANDOM':
        bond_dim = args.bond_dim
        
        A= torch.rand((model.phys_dim, bond_dim, bond_dim, bond_dim, bond_dim),\
            dtype=cfg.global_args.dtype, device=cfg.global_args.device)
        A= make_c4v_symm(A)
        A= A/torch.max(torch.abs(A))

        sites= {(0,0): A}

        state= IPEPS(sites)
    else:
        raise ValueError("Missing trial state: -instate=None and -ipeps_init_type= "\
            +str(args.ipeps_init_type)+" is not supported")

    print(state)
    
    def ctmrg_conv_specdistC(state, env, history, ctm_args=cfg.ctm_args):
        with torch.no_grad():
            u,s1,v= torch.svd(env.C[env.keyC], compute_uv=False)
            history.append(s1.tolist())

            if len(history) > 1: 
                s0 = torch.tensor(history[-2],dtype=s1.dtype,device=s1.device)
                return torch.dist(s1,s0) < ctm_args.ctm_conv_tol
        return False

    # def ctmrg_conv_energy(state, env, history, ctm_args=cfg.ctm_args):
    #     with torch.no_grad():
    #         e_curr = model.energy_1x1(state, env)
    #         history.append(e_curr.item())

    #         if len(history) > 1 and abs(history[-1]-history[-2]) < ctm_args.ctm_conv_tol:
    #             return True
    #     return False

    ctm_env= ENV_C4V(args.chi, state)
    init_env(state, ctm_env)
    ctm_env, *ctm_log= ctmrg_c4v.run(state, ctm_env, conv_check=ctmrg_conv_specdistC)

    loss= model.energy_1x1(state, ctm_env)
    obs_values, obs_labels= model.eval_obs(state,ctm_env)
    print(", ".join(["epoch","energy"]+obs_labels))
    print(", ".join([f"{-1}",f"{loss}"]+[f"{v}" for v in obs_values]))

    def loss_fn(state, ctm_env_in, opt_args=cfg.opt_args):
        # symmetrize on-site tensor
        symm_sites= {(0,0): make_c4v_symm(state.sites[(0,0)])}
        symm_state= IPEPS(symm_sites)

        # possibly re-initialize the environment
        if opt_args.opt_ctm_reinit:
            init_env(symm_state, ctm_env_in)

        # 1) compute environment by CTMRG
        ctm_env_out, *ctm_log= ctmrg_c4v.run(symm_state, ctm_env_in, conv_check=ctmrg_conv_specdistC)
        loss = model.energy_1x1(symm_state, ctm_env_out)
        
        return (loss, ctm_env_out, *ctm_log)

    # optimize
    optimize_state(state, ctm_env, loss_fn, model, args)

    # compute final observables for the best variational state
    outputstatefile= args.out_prefix+"_state.json"
    state= read_ipeps(outputstatefile)
    ctm_env = ENV_C4V(args.chi, state)
    init_env(state, ctm_env)
    ctm_env, *ctm_log = ctmrg_c4v.run(state, ctm_env, conv_check=ctmrg_conv_specdistC)
    opt_energy = model.energy_1x1(state,ctm_env)
    obs_values, obs_labels = model.eval_obs(state,ctm_env)
    print(", ".join([f"{args.opt_max_iter}",f"{opt_energy}"]+[f"{v}" for v in obs_values]))