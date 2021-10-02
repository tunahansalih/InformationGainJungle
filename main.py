import random
from pprint import pprint

import numpy as np
import tensorflow as tf
import wandb
from tqdm import tqdm

from loss.information_gain import information_gain_loss_fn
from loss.scheduling import StepDecay
from nets.model import Routing, InformationGainRoutingResNetModel, InformationGainRoutingLeNetModel
from utils.dataset import get_dataset
from utils.helpers import routing_method, information_gain_weight_scheduler, current_learning_rate, reset_metrics

wandb.init(project="conditional-information-gain-trellis", entity="tunahansalih",
           config="config.yaml")
pprint(wandb.config)

random.seed(wandb.config["RANDOM_SEED"])
np.random.seed(wandb.config["RANDOM_SEED"])
tf.random.set_seed(wandb.config["RANDOM_SEED"])

dataset_train, dataset_validation, dataset_test = get_dataset(wandb.config)

if wandb.config["MODEL"] == "RESNET18":
    model = InformationGainRoutingResNetModel(config=wandb.config, resnet_depth=18)
elif wandb.config["MODEL"] == "RESNET18_SLIM":
    model = InformationGainRoutingResNetModel(config=wandb.config, slim=True, resnet_depth=18)
elif wandb.config["MODEL"] == "LENET":
    model = InformationGainRoutingLeNetModel(config=wandb.config)
elif wandb.config["MODEL"] == "LENET_SLIM":
    model = InformationGainRoutingLeNetModel(config=wandb.config, slim=True)
else:
    NotImplementedError()

classification_loss_fn = tf.losses.CategoricalCrossentropy(from_logits=True)

optimizer = tf.optimizers.Adam(
    learning_rate=wandb.config["LR_INITIAL"],
)
model.compile(optimizer=optimizer)

metrics = {
    "Route0": [
        tf.keras.metrics.MeanTensor() for _ in range(wandb.config["NUM_CLASSES"])
    ],
    "Route1": [
        tf.keras.metrics.MeanTensor() for _ in range(wandb.config["NUM_CLASSES"])
    ],
    "Accuracy": tf.keras.metrics.CategoricalAccuracy(),
    "TotalLoss": tf.keras.metrics.Mean(),
    "Routing0Loss": tf.keras.metrics.Mean(),
    "Routing1Loss": tf.keras.metrics.Mean(),
    "ClassificationLoss": tf.keras.metrics.Mean(),
}

if wandb.config["USE_ROUTING"]:
    information_gain_weight_scheduler = information_gain_weight_scheduler(wandb.config)
    information_gain_softmax_temperature_scheduler = StepDecay(
        wandb.config["INFORMATION_GAIN_SOFTMAX_TEMPERATURE_INITIAL"],
        wandb.config["INFORMATION_GAIN_SOFTMAX_TEMPERATURE_DECAY_RATE"], decay_step=2
    )
    information_gain_balance_coefficient_scheduler = StepDecay(
        wandb.config["INFORMATION_GAIN_BALANCE_COEFFICIENT_INITIAL"],
        wandb.config["INFORMATION_GAIN_BALANCE_COEFFICIENT_DECAY_RATE"],
        decay_step=2)

global_step = 0
for epoch in range(wandb.config["NUM_EPOCHS"]):
    print(f"Epoch {epoch}")

    reset_metrics(metrics)
    progress_bar = tqdm(dataset_train)

    for i, (x_batch_train, y_batch_train) in enumerate(progress_bar):
        current_lr = current_learning_rate(global_step, wandb.config)
        tf.keras.backend.set_value(optimizer.learning_rate, current_lr)

        current_routing = routing_method(step=global_step, config=wandb.config)
        if wandb.config["USE_ROUTING"]:
            information_gain_loss_weight = information_gain_weight_scheduler.get_current_value(global_step)
            information_gain_softmax_temperature = information_gain_softmax_temperature_scheduler.get_current_value(
                step=global_step)
            information_gain_balance_coefficient = information_gain_balance_coefficient_scheduler.get_current_value(
                step=global_step)
        else:
            information_gain_loss_weight = 0
            information_gain_softmax_temperature = 1
            information_gain_balance_coefficient = 1

        with tf.GradientTape(persistent=True) as tape:
            routing_0_loss = 0
            routing_1_loss = 0
            route_0, route_1, logits = model(
                x_batch_train,
                routing=current_routing,
                temperature=information_gain_softmax_temperature,
                training=True,
            )
            classification_loss = classification_loss_fn(y_batch_train, logits)

            if (
                    wandb.config["USE_ROUTING"]
                    and current_routing == Routing.INFORMATION_GAIN_ROUTING
            ):
                route_0 = tf.nn.softmax(route_0, axis=-1)
                route_1 = tf.nn.softmax(route_1, axis=-1)
                routing_0_loss = (
                        information_gain_loss_weight
                        * information_gain_loss_fn(p_c_given_x_2d=y_batch_train,
                                                   p_n_given_x_2d=route_0,
                                                   balance_coefficient=information_gain_balance_coefficient)
                )
                routing_1_loss = (
                        information_gain_loss_weight
                        * information_gain_loss_fn(p_c_given_x_2d=y_batch_train,
                                                   p_n_given_x_2d=route_1,
                                                   balance_coefficient=information_gain_balance_coefficient)
                )
            else:
                routing_0_loss = 0
                routing_1_loss = 0

            loss_value = classification_loss + routing_0_loss + routing_1_loss

        if wandb.config["USE_ROUTING"] and wandb.config["DECOUPLE_ROUTING_GRADIENTS"]:
            model_trainable_weights = model.F_0.trainable_weights
            model_trainable_weights.extend([weight for block in model.F_1 for weight in block.trainable_weights])
            model_trainable_weights.extend([weight for block in model.F_2 for weight in block.trainable_weights])
            model_trainable_weights.extend(model.F_3.trainable_weights)

            grads = tape.gradient(classification_loss, model_trainable_weights)
            optimizer.apply_gradients(zip(grads, model_trainable_weights))

            if (
                    wandb.config["USE_ROUTING"]
                    and current_routing == Routing.INFORMATION_GAIN_ROUTING
            ):
                grads = tape.gradient(
                    routing_0_loss, model.H_0.trainable_weights
                )
                optimizer.apply_gradients(
                    zip(grads, model.H_0.trainable_weights)
                )

                grads = tape.gradient(
                    routing_1_loss, model.H_1.trainable_weights
                )
                optimizer.apply_gradients(
                    zip(grads, model.H_1.trainable_weights)
                )
        else:
            grads = tape.gradient(loss_value, model.trainable_weights)
            optimizer.apply_gradients(zip(grads, model.trainable_weights))

        del tape
        # Update metrics
        metrics["Accuracy"].update_state(
            tf.argmax(y_batch_train, axis=-1), tf.argmax(logits, axis=-1)
        )
        metrics["TotalLoss"].update_state(loss_value)
        metrics["Routing0Loss"].update_state(routing_0_loss)
        metrics["Routing1Loss"].update_state(routing_1_loss)
        metrics["ClassificationLoss"].update_state(classification_loss)

        # Log metrics
        if global_step % 100 == 0:
            progress_bar.set_description(
                f"Training Accuracy: %{metrics['Accuracy'].result().numpy() * 100:.2f} Loss: {metrics['TotalLoss'].result().numpy():.5f}"
            )

        global_step += 1

        if wandb.config["USE_ROUTING"]:
            wandb.log(
                {
                    "Training/TotalLoss": metrics["TotalLoss"].result().numpy(),
                    "Training/ClassificationLoss": metrics["ClassificationLoss"]
                        .result()
                        .numpy(),
                    "Training/Routing_0_Loss": metrics["Routing0Loss"].result().numpy(),
                    "Training/Routing_1_Loss": metrics["Routing1Loss"].result().numpy(),
                    "Training/Routing_Loss_Weight": information_gain_loss_weight,
                    "Training/Accuracy": metrics["Accuracy"].result().numpy(),
                    "Training/InformationGainSoftmaxTemperature": information_gain_softmax_temperature,
                    "Training/LearningRate": current_lr,
                    "Training/Routing": current_routing.value,
                    "Epoch": epoch
                },
                step=global_step - 1,
            )
        else:
            wandb.log(
                {
                    "Training/TotalLoss": metrics["TotalLoss"].result().numpy(),
                    "Training/ClassificationLoss": metrics["ClassificationLoss"]
                        .result()
                        .numpy(),
                    "Training/Accuracy": metrics["Accuracy"].result().numpy(),
                    "Training/SoftmaxSmoothing": information_gain_softmax_temperature,
                    "Training/LearningRate": current_lr,
                    "Epoch": epoch
                },
                step=global_step - 1,
            )

    # Validation
    if (epoch + 1) % 10 == 0 or (epoch + 1) == wandb.config["NUM_EPOCHS"]:
        reset_metrics(metrics)
        progress_bar = tqdm(dataset_validation)
        current_routing = routing_method(step=global_step - 1, config=wandb.config)
        for (x_batch_val, y_batch_val) in progress_bar:
            route_0, route_1, logits = model(
                x_batch_val, routing=current_routing, training=False
            )
            y_batch_val_index = tf.argmax(y_batch_val, axis=-1)
            y_pred_batch_val_index = tf.argmax(logits, axis=-1)
            if current_routing in [
                Routing.RANDOM_ROUTING,
                Routing.INFORMATION_GAIN_ROUTING,
            ]:
                route_0 = tf.nn.softmax(route_0, axis=-1)
                route_1 = tf.nn.softmax(route_1, axis=-1)

            for c, r_0, r_1 in zip(y_batch_val_index, route_0, route_1):
                metrics["Route0"][c].update_state(r_0)
                metrics["Route1"][c].update_state(r_1)

            metrics["Accuracy"].update_state(y_batch_val_index, y_pred_batch_val_index)

            progress_bar.set_description(
                f"Validation Accuracy: %{metrics['Accuracy'].result().numpy() * 100:.2f}"
            )

            result_log = {}
            if wandb.config["USE_ROUTING"]:
                for k in ["Route0", "Route1"]:
                    for c, metric in enumerate(metrics[k]):
                        data = [
                            [path, ratio]
                            for (path, ratio) in enumerate(metric.result().numpy())
                        ]
                        table = wandb.Table(data=data, columns=["Route", "Ratio"])
                        result_log[f"Validation/{k}/Class_{c}"] = wandb.plot.bar(
                            table, "Route", "Ratio", title=f"{k} Ratios For Class {c}"
                        )
            result_log["Validation/Accuracy"] = metrics["Accuracy"].result().numpy()
            wandb.log(result_log, step=global_step - 1)

reset_metrics(metrics)
progress_bar = tqdm(dataset_test)
current_routing = routing_method(step=global_step - 1, config=wandb.config)
for (x_batch_test, y_batch_test) in progress_bar:
    route_0, route_1, logits = model(
        x_batch_test, routing=current_routing, training=False
    )
    y_batch_val_index = tf.argmax(y_batch_test, axis=-1)
    y_pred_batch_val_index = tf.argmax(logits, axis=-1)
    if current_routing in [
        Routing.RANDOM_ROUTING,
        Routing.INFORMATION_GAIN_ROUTING,
    ]:
        route_0 = tf.nn.softmax(route_0, axis=-1)
        route_1 = tf.nn.softmax(route_1, axis=-1)

    for c, r_0, r_1 in zip(y_batch_val_index, route_0, route_1):
        metrics["Route0"][c].update_state(r_0)
        metrics["Route1"][c].update_state(r_1)

    metrics["Accuracy"].update_state(y_batch_val_index, y_pred_batch_val_index)

    progress_bar.set_description(
        f"Test Accuracy: %{metrics['Accuracy'].result().numpy() * 100:.2f}"
    )

    result_log = {}
    if wandb.config["USE_ROUTING"]:
        for k in ["Route0", "Route1"]:
            for c, metric in enumerate(metrics[k]):
                data = [
                    [path, ratio]
                    for (path, ratio) in enumerate(metric.result().numpy())
                ]
                table = wandb.Table(data=data, columns=["Route", "Ratio"])
                result_log[f"Test/{k}/Class_{c}"] = wandb.plot.bar(
                    table, "Route", "Ratio", title=f"{k} Ratios For Class {c}"
                )
    result_log["Test/Accuracy"] = metrics["Accuracy"].result().numpy()
    wandb.log(result_log, step=global_step - 1)
