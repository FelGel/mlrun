import os
import pytest
from tests.api.runtimes.base import TestRuntimeBase
from mlrun.runtimes.kubejob import KubejobRuntime
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from mlrun.runtimes.utils import generate_resources
from mlrun.platforms import auto_mount
import unittest.mock
from mlrun.api.utils.singletons.k8s import get_k8s
from mlrun.utils.vault import VaultStore
from kubernetes import client
from mlrun.config import config as mlconf


class TestKubejobRuntime(TestRuntimeBase):
    @pytest.fixture(autouse=True)
    def setup_method_fixture(self, db: Session, client: TestClient):
        # We want this mock for every test, ideally we would have simply put it in the custom_setup
        # but this function is called by the base class's setup_method which is happening before the fixtures
        # initialization. We need the client fixture (which needs the db one) in order to be able to mock k8s stuff
        self._mock_create_namespaced_pod()

    def custom_setup(self):
        self.image_name = "mlrun/mlrun:latest"
        self.vault_secrets = ["secret1", "secret2", "AWS_KEY"]
        self.vault_secret_name = "test-secret"

    def _generate_runtime(self):
        runtime = KubejobRuntime()
        runtime.spec.image = self.image_name
        return runtime

    def test_run_without_runspec(self, db: Session, client: TestClient):
        runtime = self._generate_runtime()
        self._execute_run(runtime)
        self._assert_pod_create_called()

        params = {"p1": "v1", "p2": 20}
        inputs = {"input1": f"{self.artifact_path}/input1.txt"}

        self._execute_run(runtime, params=params, inputs=inputs)
        self._assert_pod_create_called(expected_params=params, expected_inputs=inputs)

    def test_run_with_runspec(self, db: Session, client: TestClient):
        task = self._generate_task()
        params = {"p1": "v1", "p2": 20}
        task.with_params(**params)
        inputs = {
            "input1": f"{self.artifact_path}/input1.txt",
            "input2": f"{self.artifact_path}/input2.csv",
        }
        for key in inputs:
            task.with_input(key, inputs[key])
        hyper_params = {"p2": [1, 2, 3]}
        task.with_hyper_params(hyper_params, "min.loss")
        secret_source = {
            "kind": "inline",
            "source": {"secret1": "password1", "secret2": "password2"},
        }
        task.with_secrets(secret_source["kind"], secret_source["source"])

        runtime = self._generate_runtime()
        self._execute_run(runtime, runspec=task)
        self._assert_pod_create_called(
            expected_params=params,
            expected_inputs=inputs,
            expected_hyper_params=hyper_params,
            expected_secrets=secret_source,
        )

    def test_run_with_resource_limits_and_requests(
        self, db: Session, client: TestClient
    ):
        runtime = self._generate_runtime()

        gpu_type = "test/gpu"
        expected_limits = generate_resources(2, 4, 4, gpu_type)
        runtime.with_limits(
            mem=expected_limits["memory"],
            cpu=expected_limits["cpu"],
            gpus=expected_limits[gpu_type],
            gpu_type=gpu_type,
        )

        expected_requests = generate_resources(mem=2, cpu=3)
        runtime.with_requests(
            mem=expected_requests["memory"], cpu=expected_requests["cpu"]
        )

        self._execute_run(runtime)
        self._assert_pod_create_called(
            expected_limits=expected_limits, expected_requests=expected_requests
        )

    def test_run_with_mounts(self, db: Session, client: TestClient):
        runtime = self._generate_runtime()

        # Mount v3io - Set the env variable, so auto_mount() will pick it up and mount v3io
        v3io_access_key = "1111-2222-3333-4444"
        v3io_user = "test-user"
        os.environ["V3IO_ACCESS_KEY"] = v3io_access_key
        os.environ["V3IO_USERNAME"] = v3io_user
        runtime.apply(auto_mount())

        self._execute_run(runtime)
        self._assert_pod_create_called()
        self._assert_v3io_mount_configured(v3io_user, v3io_access_key)

        # Mount a PVC. Create a new runtime so we don't have both v3io and the PVC mounted
        runtime = self._generate_runtime()
        pvc_name = "test-pvc"
        pvc_mount_path = "/volume/mount/path"
        volume_name = "test-volume-name"
        runtime.apply(auto_mount(pvc_name, pvc_mount_path, volume_name))

        self._execute_run(runtime)
        self._assert_pod_create_called()
        self._assert_pvc_mount_configured(pvc_name, pvc_mount_path, volume_name)

    # For now Vault is only supported in KubeJob, so it's here. Once it's relevant to other runtimes, this can
    # move to the base.
    def _mock_vault_functionality(self):
        secret_dict = {key: "secret" for key in self.vault_secrets}
        VaultStore.get_secrets = unittest.mock.Mock(return_value=secret_dict)

        object_meta = client.V1ObjectMeta(
            name="test-service-account", namespace=self.namespace
        )
        secret = client.V1ObjectReference(
            name=self.vault_secret_name, namespace=self.namespace
        )
        service_account = client.V1ServiceAccount(
            metadata=object_meta, secrets=[secret]
        )
        get_k8s().v1api.read_namespaced_service_account = unittest.mock.Mock(
            return_value=service_account
        )

    def test_run_with_vault_secrets(self, db: Session, client: TestClient):
        self._mock_vault_functionality()
        runtime = self._generate_runtime()

        task = self._generate_task()

        task.metadata.project = self.project
        secret_source = {
            "kind": "vault",
            "source": {"project": self.project, "secrets": self.vault_secrets},
        }
        task.with_secrets(secret_source["kind"], self.vault_secrets)
        vault_url = "/url/for/vault"
        mlconf.secret_stores.vault.remote_url = vault_url
        mlconf.secret_stores.vault.token_path = vault_url

        self._execute_run(runtime, runspec=task)

        self._assert_pod_create_called(
            expected_secrets=secret_source,
            expected_env={
                "MLRUN_SECRET_STORES__VAULT__ROLE": f"project:{self.project}",
                "MLRUN_SECRET_STORES__VAULT__URL": vault_url,
            },
        )

        self._assert_secret_mount(
            "vault-secret", self.vault_secret_name, 420, vault_url
        )

    def test_run_with_code(self, db: Session, client: TestClient):
        runtime = self._generate_runtime()

        expected_code = """
def my_func(context):
    print("Hello cruel world")
        """
        runtime.with_code(body=expected_code)

        self._execute_run(runtime)
        self._assert_pod_create_called(expected_code=expected_code)

    def test_set_env(self, db: Session, client: TestClient):
        runtime = self._generate_runtime()
        env = {"MLRUN_LOG_LEVEL": "DEBUG", "IMAGE_HEIGHT": "128"}
        for env_variable in env:
            runtime.set_env(env_variable, env[env_variable])
        self._execute_run(runtime)
        self._assert_pod_create_called(expected_env=env)

        # set the same env key for a different value and check that the updated one is used
        env2 = {"MLRUN_LOG_LEVEL": "ERROR", "IMAGE_HEIGHT": "128"}
        runtime.set_env("MLRUN_LOG_LEVEL", "ERROR")
        self._execute_run(runtime)
        self._assert_pod_create_called(expected_env=env2)
