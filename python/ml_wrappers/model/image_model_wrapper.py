# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

"""Defines wrappers for vision-based models."""

import logging
from typing import Any

import numpy as np
import pandas as pd
from ml_wrappers.common.constants import ModelTask
from ml_wrappers.dataset.dataset_wrapper import DatasetWrapper
from ml_wrappers.model.evaluator import _eval_model
from ml_wrappers.model.pytorch_wrapper import WrappedPytorchModel
from ml_wrappers.model.wrapped_classification_model import \
    WrappedClassificationModel

module_logger = logging.getLogger(__name__)
module_logger.setLevel(logging.INFO)


try:
    import torch
    import torch.nn as nn
    import torchvision
except ImportError:
    module_logger.debug('Could not import torch, required if using a PyTorch model')

try:
    from mlflow.pyfunc import PyFuncModel
except ImportError:
    PyFuncModel = Any
    module_logger.debug('Could not import mlflow, required if using an mlflow model')

try:
    from torchvision.transforms import ToTensor
except ImportError:
    module_logger.debug('Could not import torchvision, required if using' +
                        ' a vision PyTorch model')

FASTAI_MODEL_SUFFIX = "fastai.learner.Learner'>"


def _is_fastai_model(model):
    """Check if the model is a fastai model.

    :param model: The model to check.
    :type model: object
    :return: True if the model is a fastai model, False otherwise.
    :rtype: bool
    """
    return str(type(model)).endswith(FASTAI_MODEL_SUFFIX)


def _wrap_image_model(model, examples, model_task, is_function):
    """If needed, wraps the model or function in a common API.

    Wraps the model based on model task and prediction function contract.

    :param model: The model or function to evaluate on the examples.
    :type model: function or model to wrap
    :param examples: The model evaluation examples.
    :type examples: ml_wrappers.DatasetWrapper or numpy.ndarray
        or pandas.DataFrame or panads.Series or scipy.sparse.csr_matrix
        or shap.DenseData or torch.Tensor
    :param model_task: Parameter to specify whether the model is an
        'image_classification' or another type of image model.
    :type model_task: str
    :return: The function chosen from given model and chosen domain, or
        model wrapping the function and chosen domain.
    :rtype: (function, str) or (model, str)
    """
    _wrapped_model = model
    if model_task == ModelTask.IMAGE_CLASSIFICATION:
        try:
            if isinstance(model, nn.Module):
                model = WrappedPytorchModel(model, image_to_tensor=True)
                if not isinstance(examples, DatasetWrapper):
                    examples = DatasetWrapper(examples)
                eval_function, eval_ml_domain = _eval_model(model, examples, model_task)
                return (
                    WrappedClassificationModel(model, eval_function, examples),
                    eval_ml_domain,
                )
        except (NameError, AttributeError):
            module_logger.debug(
                'Could not import torch, required if using a pytorch model'
            )

        if _is_fastai_model(model):
            _wrapped_model = WrappedFastAIImageClassificationModel(model)
        elif hasattr(model, '_model_impl'):
            if str(type(model._model_impl.python_model)).endswith(
                "azureml.automl.dnn.vision.common.mlflow.mlflow_model_wrapper.MLFlowImagesModelWrapper'>"
            ):
                _wrapped_model = WrappedMlflowAutomlImagesClassificationModel(model)
        else:
            _wrapped_model = WrappedTransformerImageClassificationModel(model)
    elif model_task == ModelTask.MULTILABEL_IMAGE_CLASSIFICATION:
        if _is_fastai_model(model):
            _wrapped_model = WrappedFastAIImageClassificationModel(
                model, multilabel=True
            )
    elif model_task == ModelTask.OBJECT_DETECTION:
        _wrapped_model = WrappedObjectDetectionModel(model)
    return _wrapped_model, model_task


class WrappedTransformerImageClassificationModel(object):
    """A class for wrapping a Transformers model in the scikit-learn style."""

    def __init__(self, model):
        """Initialize the WrappedTransformerImageClassificationModel."""
        self._model = model

    def predict(self, dataset):
        """Predict the output using the wrapped Transformers model.

        :param dataset: The dataset to predict on.
        :type dataset: ml_wrappers.DatasetWrapper
        :return: The predicted values.
        :rtype: numpy.ndarray
        """
        return np.argmax(self._model(dataset), axis=1)

    def predict_proba(self, dataset):
        """Predict the output probability using the Transformers model.

        :param dataset: The dataset to predict_proba on.
        :type dataset: ml_wrappers.DatasetWrapper
        :return: The predicted probabilities.
        :rtype: numpy.ndarray
        """
        return self._model(dataset)


class WrappedFastAIImageClassificationModel(object):
    """A class for wrapping a FastAI model in the scikit-learn style."""

    def __init__(self, model, multilabel=False):
        """Initialize the WrappedFastAIImageClassificationModel.

        :param model: The model to wrap.
        :type model: fastai.learner.Learner
        :param multilabel: Whether the model is a multilabel model.
        :type multilabel: bool
        """
        self._model = model
        self._multilabel = multilabel

    def _fastai_predict(self, dataset, index):
        """Predict the output using the wrapped FastAI model.

        :param dataset: The dataset to predict on.
        :type dataset: ml_wrappers.DatasetWrapper
        :param index: The index into the predicted data.
            Index 1 is for the predicted class and index
            2 is for the predicted probability.
        :type index: int
        :return: The predicted data.
        :rtype: numpy.ndarray
        """
        # Note predict for single image requires 3d instead of 4d array
        if len(dataset.shape) == 4:
            predictions = []
            for row in dataset:
                predictions.append(self._fastai_predict(row, index))
            predictions = np.array(predictions)
            if index == 1 and not self._multilabel:
                predictions = predictions.flatten()
            return predictions
        else:
            predictions = np.array(self._model.predict(dataset)[index])
            if len(predictions.shape) == 0:
                predictions = predictions.reshape(1)
            if index == 1:
                is_boolean = predictions.dtype == bool
                if is_boolean:
                    predictions = predictions.astype(int)
            return predictions

    def predict(self, dataset):
        """Predict the output value using the wrapped FastAI model.

        :param dataset: The dataset to predict on.
        :type dataset: ml_wrappers.DatasetWrapper
        :return: The predicted values.
        :rtype: numpy.ndarray
        """
        return self._fastai_predict(dataset, 1)

    def predict_proba(self, dataset):
        """Predict the output probability using the FastAI model.

        :param dataset: The dataset to predict_proba on.
        :type dataset: ml_wrappers.DatasetWrapper
        :return: The predicted probabilities.
        :rtype: numpy.ndarray
        """
        return self._fastai_predict(dataset, 2)


class WrappedMlflowAutomlImagesClassificationModel:
    """A class for wrapping an AutoML for images MLflow model in the scikit-learn style."""

    def __init__(self, model: PyFuncModel) -> None:
        """Initialize the WrappedMlflowAutomlImagesClassificationModel.

        :param model: mlflow model
        :type model: mlflow.pyfunc.PyFuncModel
        """
        self._model = model

    def _mlflow_predict(self, dataset: pd.DataFrame) -> pd.DataFrame:
        """Perform the inference using the wrapped MLflow model.

        :param dataset: The dataset to predict on.
        :type dataset: pandas.DataFrame
        :return: The predicted data.
        :rtype: pandas.DataFrame
        """
        predictions = self._model.predict(dataset)
        return predictions

    def predict(self, dataset: pd.DataFrame) -> np.ndarray:
        """Predict the output value using the wrapped MLflow model.

        :param dataset: The dataset to predict on.
        :type dataset: pandas.DataFrame
        :return: The predicted values.
        :rtype: numpy.ndarray
        """
        predictions = self._mlflow_predict(dataset)
        return predictions.loc[:, 'probs'].map(lambda x: np.argmax(x)).values

    def predict_proba(self, dataset: pd.DataFrame) -> np.ndarray:
        """Predict the output probability using the MLflow model.

        :param dataset: The dataset to predict_proba on.
        :type dataset: pandas.DataFrame
        :return: The predicted probabilities.
        :rtype: numpy.ndarray
        """
        predictions = self._mlflow_predict(dataset)
        return np.stack(predictions.probs.values)


class WrappedObjectDetectionModel:
    """A class for wrapping a object detection model in the scikit-learn style."""

    def __init__(self, model: Any) -> None:
        """Initialize the WrappedObjectDetectionModel with the model and evaluation function.

        :param model: mlflow model
        :type model: Any
        """
        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self._model = model
        # Set eval automatically for user for batchnorm and dropout layers
        self._model.eval()

    def predict(self, dataset, detection_error_threshold=0.1):
        """
        Prediction function for object detector.

        :param dataset: The dataset to predict on.
        type dataset: PIL Image or numpy.ndarray
        param device: device pytorch is writing to
        type device: pytorch device
        param detection_error_threshold: amount of acceptable error.
            objects with error scores higher than the threshold will be removed
        type detection_threshold: float
        """
        predictions = []
        for image in dataset:
            detections = self._model(ToTensor()(image).to(self._device).unsqueeze(0))

            prediction = detections[0]

            keep = torchvision.ops.nms(
                prediction["boxes"],
                prediction["scores"],
                detection_error_threshold,
            )

            prediction["boxes"] = prediction["boxes"][keep]
            prediction["scores"] = prediction["scores"][keep]
            prediction["labels"] = prediction["labels"][keep]

            image_predictions = torch.cat((prediction["labels"].unsqueeze(1),
                                           prediction["boxes"],
                                           prediction["scores"].unsqueeze(1)), dim=1)

            predictions.append(image_predictions.detach().cpu().numpy().tolist())

        return predictions

    def predict_proba(self, dataset, detection_error_threshold=0.05):
        """Predict the output probability using the wrapped model.

        :param dataset: The dataset to predict_proba on.
        :type dataset: ml_wrappers.DatasetWrapper
        """
        predictions = self.predict(dataset, detection_error_threshold)
        prob_scores = [[pred[-1] for pred in image_prediction] for image_prediction in predictions]
        return prob_scores
