{
  description = "An MCP proxy bridge that aggregates multiple Model Context Protocol servers behind a single HTTP endpoint";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        uv2nix.follows = "uv2nix";
        nixpkgs.follows = "nixpkgs";
      };
    };
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        nixpkgs.follows = "nixpkgs";
      };
    };
  };

  outputs = inputs:
    inputs.flake-parts.lib.mkFlake {inherit inputs;} {
      systems = ["x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin"];
      imports = [inputs.flake-parts.flakeModules.easyOverlay];
      perSystem = {
        config,
        lib,
        pkgs,
        ...
      }: let
        inherit (pkgs.callPackages inputs.pyproject-nix.build.util {}) mkApplication;
        workspace = inputs.uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./bridge;};
        overlay = workspace.mkPyprojectOverlay {
          sourcePreference = "wheel";
        };
        python = lib.head (inputs.pyproject-nix.lib.util.filterPythonInterpreters {
          inherit (workspace) requires-python;
          inherit (pkgs) pythonInterpreters;
        });
        pythonBase = pkgs.callPackage inputs.pyproject-nix.build.packages {
          inherit python;
        };
        pythonSet = pythonBase.overrideScope (
          lib.composeManyExtensions [
            inputs.pyproject-build-systems.overlays.wheel
            overlay
          ]
        );
        mcp-bridge = pythonSet.mkVirtualEnv "mcp-bridge-env" workspace.deps.default;
        mcp-bridge-bin = mkApplication {
          venv = mcp-bridge;
          package = pythonSet.mcp-bridge;
        };
        mcp-companion-nvim = pkgs.vimUtils.buildVimPlugin {
          pname = "mcp-companion-nvim";
          version = toString (inputs.self.shortRev or inputs.self.dirtyShortRev or inputs.self.lastModified or "git");
          src = with lib.fileset;
            toSource {
              root = ./.;
              fileset = unions (map maybeMissing [./lua ./plugin ./ftdetect ./doc]);
            };
        };
      in {
        packages = {
          inherit mcp-bridge mcp-bridge-bin mcp-companion-nvim;
          default = mcp-bridge-bin;
        };
        overlayAttrs = config.packages;
      };
    };
}
