// Copyright (C) 2019 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//

#include "cnn.hpp"

#include <chrono>
#include <map>
#include <string>

#include <utils/common.hpp>

namespace {
constexpr size_t MAX_NUM_DECODER = 20;
}

Cnn::Cnn(const std::string &model_path, Core & ie, const std::string & deviceName, const cv::Size &new_input_resolution) {
    // ---------------------------------------------------------------------------------------------------
    channels_ = 0;
    time_elapsed_ = 0;
    ncalls_ = 0;
    // --------------------------- 1. Reading network ----------------------------------------------------
    auto network = ie.ReadNetwork(model_path);

    // --------------------------- Changing input shape if it is needed ----------------------------------
    InputsDataMap inputInfo(network.getInputsInfo());
    if (inputInfo.size() != 1) {
        throw std::runtime_error("The network should have only one input");
    }
    InputInfo::Ptr inputInfoFirst = inputInfo.begin()->second;

    SizeVector input_dims = inputInfoFirst->getInputData()->getTensorDesc().getDims();
    input_dims[0] = 1;
    if (new_input_resolution != cv::Size()) {
        input_dims[2] = static_cast<size_t>(new_input_resolution.height);
        input_dims[3] = static_cast<size_t>(new_input_resolution.width);
    }

    std::map<std::string, SizeVector> input_shapes;
    input_shapes[network.getInputsInfo().begin()->first] = input_dims;
    network.reshape(input_shapes);

    // ---------------------------------------------------------------------------------------------------

    // --------------------------- Configuring input and output ------------------------------------------
    // ---------------------------   Preparing input blobs -----------------------------------------------
    InputInfo::Ptr input_info = network.getInputsInfo().begin()->second;
    input_name_ = network.getInputsInfo().begin()->first;

    input_info->setLayout(Layout::NCHW);
    input_info->setPrecision(Precision::U8);

    channels_ = input_info->getTensorDesc().getDims()[1];
    input_size_ = cv::Size(input_info->getTensorDesc().getDims()[3], input_info->getTensorDesc().getDims()[2]);

    // ---------------------------   Preparing output blobs ----------------------------------------------

    OutputsDataMap output_info(network.getOutputsInfo());
    for (auto output : output_info) {
        output_names_.emplace_back(output.first);
    }

    // ---------------------------------------------------------------------------------------------------

    // --------------------------- Loading model to the device -------------------------------------------
    ExecutableNetwork executable_network = ie.LoadNetwork(network, deviceName);
    // ---------------------------------------------------------------------------------------------------

    // --------------------------- Creating infer request ------------------------------------------------
    infer_request_ = executable_network.CreateInferRequest();
    // ---------------------------------------------------------------------------------------------------
}

InferenceEngine::BlobMap Cnn::Infer(const cv::Mat &frame) {
    std::chrono::steady_clock::time_point begin = std::chrono::steady_clock::now();
    cv::Mat image;
    if (channels_ == 1) {
         cv::cvtColor(frame, image, cv::COLOR_BGR2GRAY);
    } else {
        image = frame.clone();
    }

    auto blob = infer_request_.GetBlob(input_name_);
    matU8ToBlob<uint8_t>(image, blob);
    infer_request_.Infer();
    // ---------------------------------------------------------------------------------------------------

    // --------------------------- Processing output -----------------------------------------------------

    InferenceEngine::BlobMap blobs;
    for (const auto &output_name : output_names_) {
        blobs[output_name] = infer_request_.GetBlob(output_name);
    }

    std::chrono::steady_clock::time_point end = std::chrono::steady_clock::now();
    time_elapsed_ += std::chrono::duration_cast<std::chrono::milliseconds>(end - begin).count();
    ncalls_++;

    return blobs;
}

void EncoderDecoderCNN::check_net_names(const OutputsDataMap &output_info_encoder,
                                        const OutputsDataMap &output_info_decoder,
                                        const InputsDataMap &input_info_decoder
                                        ) {
    std::string name_not_exist = "";
    if (output_info_encoder.find(out_enc_hidden_name_) == output_info_encoder.end())
        name_not_exist = out_enc_hidden_name_;
    if (output_info_encoder.find(features_name_) == output_info_encoder.end())
        name_not_exist = features_name_;
    if (input_info_decoder.find(in_dec_hidden_name_) == input_info_decoder.end())
        name_not_exist = in_dec_hidden_name_;
    if (input_info_decoder.find(features_name_) == input_info_decoder.end())
        name_not_exist = features_name_;
    if (input_info_decoder.find(in_dec_symbol_name_) == input_info_decoder.end())
        name_not_exist = in_dec_symbol_name_;
    if (output_info_decoder.find(out_dec_hidden_name_) == output_info_decoder.end())
        name_not_exist = out_dec_hidden_name_;
    if (output_info_decoder.find(out_dec_symbol_name_) == output_info_decoder.end())
        name_not_exist = out_dec_symbol_name_;
    if (name_not_exist != "")
        throw NameNotExist(name_not_exist);
 }


EncoderDecoderCNN::EncoderDecoderCNN(const std::string &model_path,
                                     Core & ie, const std::string & deviceName,
                                     const std::string out_enc_hidden_name,
                                     const std::string out_dec_hidden_name,
                                     const std::string in_dec_hidden_name,
                                     const std::string features_name,
                                     const std::string in_dec_symbol_name,
                                     const std::string out_dec_symbol_name,
                                     const std::string logits_name,
                                     unsigned end_token,
                                     const cv::Size &new_input_resolution
                        ) : Cnn(model_path, ie, deviceName, new_input_resolution) {
    // ---------------------------------------------------------------------------------------------------
    // --------------------------- Setting names ---------------------------------------------------------
    out_enc_hidden_name_ = out_enc_hidden_name;
    out_dec_hidden_name_ = out_dec_hidden_name;
    in_dec_hidden_name_ = in_dec_hidden_name;
    features_name_ = features_name;
    in_dec_symbol_name_ = in_dec_symbol_name;
    out_dec_symbol_name_ = out_dec_symbol_name;
    logits_name_ = logits_name;
    // --------------------------- Checking paths --------------------------------------------------------
    std::string model_path_decoder = model_path;
    auto network_encoder = ie.ReadNetwork(model_path);
    CNNNetwork network_decoder;
    if (model_path_decoder.find("encoder") == std::string::npos)
        throw DecoderNotFound();
    while (model_path_decoder.find("encoder") != std::string::npos)
        model_path_decoder = model_path_decoder.replace(model_path_decoder.find("encoder"), 7, "decoder");
    network_decoder = ie.ReadNetwork(model_path_decoder);

    InputsDataMap inputInfo(network_encoder.getInputsInfo());
    if (inputInfo.size() != 1) {
        throw std::runtime_error("The network_encoder should have only one input");
    }
    // --------------------------- Checking net names ----------------------------------------------------
    this->check_net_names(network_encoder.getOutputsInfo(),
                        network_decoder.getOutputsInfo(),
                        network_decoder.getInputsInfo());

    // ---------------------------------------------------------------------------------------------------
    InputInfo::Ptr input_info = network_encoder.getInputsInfo().begin()->second;
    input_name_ = network_encoder.getInputsInfo().begin()->first;

    input_info->setLayout(Layout::NCHW);
    input_info->setPrecision(Precision::U8);

    channels_ = input_info->getTensorDesc().getDims()[1];
    input_size_ = cv::Size(input_info->getTensorDesc().getDims()[3], input_info->getTensorDesc().getDims()[2]);

    // --------------------------- Loading model to the device -------------------------------------------
    ExecutableNetwork executable_network_encoder = ie.LoadNetwork(network_encoder, deviceName);
    ExecutableNetwork executable_network_decoder = ie.LoadNetwork(network_decoder, deviceName);
    // ---------------------------------------------------------------------------------------------------

    // --------------------------- Creating infer request ------------------------------------------------
    infer_request_encoder_ = executable_network_encoder.CreateInferRequest();
    infer_request_decoder_ = executable_network_decoder.CreateInferRequest();
    // ---------------------------------------------------------------------------------------------------
    end_token_ = end_token;

}

InferenceEngine::BlobMap EncoderDecoderCNN::Infer(const cv::Mat &frame) {
    std::chrono::steady_clock::time_point begin = std::chrono::steady_clock::now();

    cv::Mat image;
    if (channels_ == 1) {
         cv::cvtColor(frame, image, cv::COLOR_BGR2GRAY);
    } else {
        image = frame;
    }
    matU8ToBlob<uint8_t>(image, infer_request_encoder_.GetBlob(input_name_));

    infer_request_encoder_.Infer();
    // --------------------------- Processing encoder output -----------------------------------------------------
    // blobs here are set for concrete network
    // in case of different network this needs to be changed or generalized
    infer_request_decoder_.SetBlob(features_name_, infer_request_encoder_.GetBlob(features_name_));
    infer_request_decoder_.SetBlob(in_dec_hidden_name_, infer_request_encoder_.GetBlob(out_enc_hidden_name_));

    InferenceEngine::LockedMemory<void> input_decoder =
        InferenceEngine::as<InferenceEngine::MemoryBlob>(infer_request_decoder_.GetBlob(in_dec_symbol_name_))->wmap();
    float* input_data_decoder = input_decoder.as<float *>();
    input_data_decoder[0] = 0;
    auto num_classes = infer_request_decoder_.GetBlob(out_dec_symbol_name_)->size();

    auto targets = InferenceEngine::make_shared_blob<float>(
        InferenceEngine::TensorDesc(Precision::FP32, std::vector<size_t> {1, MAX_NUM_DECODER, num_classes},
        Layout::HWC));
    targets->allocate();
    LockedMemory<void> blobMapped = targets->wmap();
    auto data_targets = blobMapped.as<float*>();

    for (size_t num_decoder = 0; num_decoder < MAX_NUM_DECODER; num_decoder ++) {
        infer_request_decoder_.Infer();
        InferenceEngine::LockedMemory<const void> output_decoder =
                     InferenceEngine::as<InferenceEngine::MemoryBlob>(infer_request_decoder_.GetBlob(out_dec_symbol_name_))->rmap();
        const float * output_data_decoder = output_decoder.as<const float *>();

        auto max_elem_vector = std::max_element(output_data_decoder, output_data_decoder + num_classes);
        auto argmax = std::distance(output_data_decoder, max_elem_vector);
        for (size_t i = 0; i < num_classes; i++)
            data_targets[num_decoder * num_classes + i] = output_data_decoder[i];
        if (end_token_ == argmax)
            break;
        input_data_decoder[0] = float(argmax);

        infer_request_decoder_.SetBlob(in_dec_hidden_name_, infer_request_decoder_.GetBlob(out_enc_hidden_name_));
    }

    std::chrono::steady_clock::time_point end = std::chrono::steady_clock::now();
    time_elapsed_ += std::chrono::duration_cast<std::chrono::milliseconds>(end - begin).count();
    ncalls_++;
    return {{logits_name_, targets}};
}
