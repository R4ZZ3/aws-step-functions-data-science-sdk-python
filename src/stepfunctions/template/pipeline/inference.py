# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed 
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either 
# express or implied. See the License for the specific language governing 
# permissions and limitations under the License.
from __future__ import absolute_import

import sagemaker

from sagemaker.utils import base_name_from_image
from sagemaker.model import Model
from sagemaker.pipeline import PipelineModel

from stepfunctions.steps import TrainingStep, TransformStep, ModelStep, EndpointConfigStep, EndpointStep, Chain, Fail, Catch
from stepfunctions.workflow import Workflow, Execution
from stepfunctions.template.pipeline.common import StepId, WorkflowTemplate


class InferencePipeline(WorkflowTemplate):

    """
    Creates a standard inference pipeline with the following steps in order:

        1. Train preprocessor
        2. Create preprocessor model
        3. Transform input data using preprocessor model  
        4. Train estimator
        5. Create estimator model
        6. Endpoint configuration
        7. Deploy estimator model
    """

    __allowed_kwargs = ('compression_type', 'content_type', 'pipeline_name')

    def __init__(self, preprocessor, estimator, inputs, s3_bucket, role, client=None, **kwargs):
        """
        Args:
            preprocessor (sagemaker.estimator.EstimatorBase): The estimator used to preprocess and transform the training data. 
            estimator (sagemaker.estimator.EstimatorBase): The estimator to use for training. Can be a BYO estimator, Framework estimator or Amazon algorithm estimator.
            role (str): An AWS IAM role (either name or full Amazon Resource Name (ARN)). This role is used to create, manage, and execute the Step Functions workflows.
            inputs: Information about the training data. Please refer to the `fit()` method of the associated estimator, as this can take any of the following forms:

                * (str) - The S3 location where training data is saved.
                * (dict[str, str] or dict[str, `sagemaker.session.s3_input`]) - If using multiple channels for training data, you can specify a dict mapping channel names to strings or `sagemaker.session.s3_input` objects.
                * (`sagemaker.session.s3_input`) - Channel configuration for S3 data sources that can provide additional information about the training dataset. See `sagemaker.session.s3_input` for full details.
                * (`sagemaker.amazon.amazon_estimator.RecordSet`) - A collection of Amazon `Record` objects serialized and stored in S3. For use with an estimator for an Amazon algorithm.
                * (list[`sagemaker.amazon.amazon_estimator.RecordSet`]) - A list of `sagemaker.amazon.amazon_estimator.RecordSet` objects, where each instance is a different channel of training data.
            s3_bucket (str): S3 bucket under which the output artifacts from the training job will be stored. The parent path used is built using the format: ``s3://{s3_bucket}/{pipeline_name}/models/{job_name}/``. In this format, `pipeline_name` refers to the keyword argument provided for TrainingPipeline. If a `pipeline_name` argument was not provided, one is auto-generated by the pipeline as `training-pipeline-<timestamp>`. Also, in the format, `job_name` refers to the job name provided when calling the :meth:`TrainingPipeline.run()` method.
            client (SFN.Client, optional): boto3 client to use for creating and interacting with the inference pipeline in Step Functions. (default: None)

        Keyword Args:
            compression_type (str, optional): Compression type (Gzip/None) of the file for TransformJob. (default:None)
            content_type (str, optional): Content type (MIME) of the document to be used in preprocessing script. See SageMaker documentation for more details. (default:None)
            pipeline_name (str, optional): Name of the pipeline. This name will be used to name jobs (if not provided when calling execute()), models, endpoints, and S3 objects created by the pipeline. If a `pipeline_name` argument was not provided, one is auto-generated by the pipeline as `training-pipeline-<timestamp>`. (default:None)
        """
        self.preprocessor = preprocessor
        self.estimator = estimator
        self.inputs = inputs
        self.s3_bucket = s3_bucket

        for key in self.__class__.__allowed_kwargs:
            setattr(self, key, kwargs.pop(key, None))

        if not self.pipeline_name:
            self.pipeline_name = 'inference-pipeline-{date}'.format(date=self._generate_timestamp())

        self.definition = self.build_workflow_definition()
        self.input_template = self._extract_input_template(self.definition)

        workflow = Workflow(name=self.pipeline_name, definition=self.definition, role=role, format_json=True, client=client)

        super(InferencePipeline, self).__init__(s3_bucket=s3_bucket, workflow=workflow, role=role, client=client)

    def build_workflow_definition(self):
        """
        Build the workflow definition for the inference pipeline with all the states involved.

        Returns:
            :class:`~stepfunctions.steps.states.Chain`: Workflow definition as a chain of states involved in the the inference pipeline.
        """
        default_name = self.pipeline_name

        train_instance_type = self.preprocessor.train_instance_type
        train_instance_count = self.preprocessor.train_instance_count

        # Preprocessor for feature transformation
        preprocessor_train_step = TrainingStep(
            StepId.TrainPreprocessor.value,
            estimator=self.preprocessor,
            job_name=default_name + '/preprocessor-source',
            data=self.inputs,
        )
        preprocessor_model = self.preprocessor.create_model()
        preprocessor_model_step = ModelStep(
            StepId.CreatePreprocessorModel.value,
            instance_type=train_instance_type,
            model=preprocessor_model,
            model_name=default_name
        )
        preprocessor_transform_step = TransformStep(
            StepId.TransformInput.value,
            transformer=self.preprocessor.transformer(instance_count=train_instance_count, instance_type=train_instance_type, max_payload=20),
            job_name=default_name,
            model_name=default_name,
            data=self.inputs['train'],
            compression_type=self.compression_type,
            content_type=self.content_type
        )

        # Training
        train_instance_type = self.estimator.train_instance_type
        train_instance_count = self.estimator.train_instance_count

        training_step = TrainingStep(
            StepId.Train.value,
            estimator=self.estimator,
            job_name=default_name + '/estimator-source',
            data=self.inputs,
        )

        pipeline_model = PipelineModel(
            name='PipelineModel',
            role=self.estimator.role,
            models=[
                self.preprocessor.create_model(),
                self.estimator.create_model()
            ]
        )
        pipeline_model_step = ModelStep(
            StepId.CreatePipelineModel.value,
            instance_type=train_instance_type,
            model=preprocessor_model,
            model_name=default_name
        )
        pipeline_model_step.parameters = self.pipeline_model_config(train_instance_type, pipeline_model)

        deployable_model = Model(model_data='', image='')

        # Deployment
        endpoint_config_step = EndpointConfigStep(
            StepId.ConfigureEndpoint.value,
            endpoint_config_name=default_name,
            model_name=default_name,
            initial_instance_count=train_instance_count,
            instance_type=train_instance_type
        )

        deploy_step = EndpointStep(
            StepId.Deploy.value,
            endpoint_name=default_name,
            endpoint_config_name=default_name,
        )

        return Chain([
            preprocessor_train_step,
            preprocessor_model_step,
            preprocessor_transform_step,
            training_step,
            pipeline_model_step,
            endpoint_config_step,
            deploy_step
        ])

    def pipeline_model_config(self, instance_type, pipeline_model):
        return {
            'ModelName': 'pipeline-model',
            'Containers': pipeline_model.pipeline_container_def(instance_type),
            'ExecutionRoleArn': pipeline_model.role
        }

    def replace_sagemaker_job_name(self, config, job_name):
        if sagemaker.model.JOB_NAME_PARAM_NAME in config['HyperParameters']:
            config['HyperParameters'][sagemaker.model.JOB_NAME_PARAM_NAME] = '"{}"'.format(job_name)

    def execute(self, job_name=None, hyperparameters=None):
        """
        Run the inference pipeline.

        Args:
            job_name (str, optional): Name for the training job. This is also used as suffix for the preprocessing job as `preprocess-<job_name>`. If one is not provided, a job name will be auto-generated. (default: None)
            hyperparameters (dict, optional): Hyperparameters for the estimator training. (default: None)

        Returns:
            :py:class:`~stepfunctions.workflow.Execution`: Running instance of the inference pipeline.
        """
        inputs = self.input_template.copy()

        if hyperparameters is not None:
            inputs[StepId.Train.value]['HyperParameters'] = hyperparameters
        
        if job_name is None:
            job_name = '{base_name}-{timestamp}'.format(base_name='inference-pipeline', timestamp=self._generate_timestamp())

        # Configure preprocessor
        inputs[StepId.TrainPreprocessor.value]['TrainingJobName'] = 'preprocessor-' + job_name
        inputs[StepId.TrainPreprocessor.value]['OutputDataConfig']['S3OutputPath'] = 's3://{s3_bucket}/{pipeline_name}/models'.format(
            s3_bucket=self.s3_bucket,
            pipeline_name=self.workflow.name
        )
        inputs[StepId.TrainPreprocessor.value]['DebugHookConfig']['S3OutputPath'] = 's3://{s3_bucket}/{pipeline_name}/models/debug'.format(
            s3_bucket=self.s3_bucket,
            pipeline_name=self.workflow.name
        )
        inputs[StepId.CreatePreprocessorModel.value]['PrimaryContainer']['ModelDataUrl'] = '{s3_uri}/{job}/output/model.tar.gz'.format(
            s3_uri=inputs[StepId.TrainPreprocessor.value]['OutputDataConfig']['S3OutputPath'],
            job=inputs[StepId.TrainPreprocessor.value]['TrainingJobName']
        )
        inputs[StepId.CreatePreprocessorModel.value]['ModelName'] = inputs[StepId.TrainPreprocessor.value]['TrainingJobName']
        inputs[StepId.TransformInput.value]['ModelName'] = inputs[StepId.CreatePreprocessorModel.value]['ModelName']
        inputs[StepId.TransformInput.value]['TransformJobName'] = inputs[StepId.CreatePreprocessorModel.value]['ModelName']
        inputs[StepId.TransformInput.value]['TransformOutput']['S3OutputPath'] = 's3://{s3_bucket}/{pipeline_name}/{transform_job}/transform'.format(
            s3_bucket=self.s3_bucket,
            pipeline_name=self.workflow.name,
            transform_job='preprocessor-transform-' + job_name
        )
        self.replace_sagemaker_job_name(inputs[StepId.TrainPreprocessor.value], inputs[StepId.TrainPreprocessor.value]['TrainingJobName'])
        
        # Configure training and model
        inputs[StepId.Train.value]['TrainingJobName'] = 'estimator-' + job_name
        inputs[StepId.Train.value]['InputDataConfig'] = [{
            'ChannelName': 'train',
            'DataSource': {
                'S3DataSource': {
                    'S3DataDistributionType': 'FullyReplicated',
                    'S3DataType': 'S3Prefix',
                    'S3Uri': '{s3_uri}'.format(
                        s3_uri=inputs[StepId.TransformInput.value]['TransformOutput']['S3OutputPath']
                    )
                }
            }
        }]
        inputs[StepId.Train.value]['OutputDataConfig']['S3OutputPath'] = 's3://{s3_bucket}/{pipeline_name}/models'.format(
            s3_bucket=self.s3_bucket,
            pipeline_name=self.workflow.name
        )
        inputs[StepId.Train.value]['DebugHookConfig']['S3OutputPath'] = 's3://{s3_bucket}/{pipeline_name}/models/debug'.format(
            s3_bucket=self.s3_bucket,
            pipeline_name=self.workflow.name
        )
        inputs[StepId.CreatePipelineModel.value]['ModelName'] = job_name
        self.replace_sagemaker_job_name(inputs[StepId.Train.value], inputs[StepId.Train.value]['TrainingJobName'])

        # Configure pipeline model
        inputs[StepId.CreatePipelineModel.value]['Containers'][0]['ModelDataUrl'] = inputs[StepId.CreatePreprocessorModel.value]['PrimaryContainer']['ModelDataUrl']
        inputs[StepId.CreatePipelineModel.value]['Containers'][1]['ModelDataUrl'] = '{s3_uri}/{job}/output/model.tar.gz'.format(
            s3_uri=inputs[StepId.Train.value]['OutputDataConfig']['S3OutputPath'],
            job=inputs[StepId.Train.value]['TrainingJobName']
        )

        # Configure endpoint
        inputs[StepId.ConfigureEndpoint.value]['EndpointConfigName'] = job_name
        for variant in inputs[StepId.ConfigureEndpoint.value]['ProductionVariants']:
            variant['ModelName'] = job_name
        inputs[StepId.Deploy.value]['EndpointConfigName'] = job_name
        inputs[StepId.Deploy.value]['EndpointName'] = job_name
        
        return self.workflow.execute(inputs=inputs, name=job_name)
